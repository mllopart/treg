"""Deferred settlement for asynchronous metered catalog calls."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlmodel import select

from treg.application import asynctasks as task_app
from treg.application.call import service as call_service
from treg.application.call.types import UpstreamResponse
from treg.config import get_settings
from treg import archive, audit, reconcile
from treg.domain import asynctasks
from treg.domain import money as ledger
from treg.domain.money import settlement
from treg.infra.db import session_maker
from treg.models import ArchiveKey, ArchiveSnapshot, AsyncTaskRecord, Hold, LedgerEntry
from treg.timeutil import utcnow_naive


EP = "replicate.image-gen.flux-schnell"


def _response(status: int, document: dict) -> UpstreamResponse:
    body = json.dumps(document).encode()

    async def stream():
        yield body

    async def close():
        return None

    return UpstreamResponse(status, ((b"content-type", b"application/json"),), stream(), close)


@pytest.fixture
def replicate_platform(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_REPLICATE", "test-platform-token")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "replicate")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _submit(clients: AsyncClient, monkeypatch, document: dict):
    async def fake_relay(*args, **kwargs):
        return _response(201, document)

    monkeypatch.setattr(call_service, "relay", fake_relay)
    return await clients.post(f"/call/{EP}", json={"input": {
        "prompt": "A red kite over a beach.", "num_outputs": 1,
        "aspect_ratio": "1:1", "output_format": "webp",
    }})


async def test_settle_fork_keeps_hold_and_writes_pending_row(
    clients: AsyncClient, monkeypatch, replicate_platform,
):
    response = await _submit(clients, monkeypatch, {
        "id": "prediction-1", "urls": {"get": "https://api.replicate.com/v1/predictions/1"}})
    assert response.status_code == 201
    assert response.headers["X-Treg-Cost-Micro"] == "3000"
    call_id = response.headers["X-Treg-Call-Id"]
    async with session_maker() as db:
        row = await db.get(AsyncTaskRecord, call_id)
        hold = await db.get(Hold, call_id)
    assert row is not None and hold is not None
    assert (row.task_id, row.poll_url, row.status) == ("prediction-1", None, "pending")
    assert row.settlement_basis["when"] == "terminal"


async def test_extraction_failure_is_persisted_fail_closed(
    clients: AsyncClient, monkeypatch, replicate_platform,
):
    response = await _submit(clients, monkeypatch, {})
    assert response.status_code == 201
    async with session_maker() as db:
        row = await db.get(AsyncTaskRecord, response.headers["X-Treg-Call-Id"])
        hold = await db.get(Hold, response.headers["X-Treg-Call-Id"])
    assert row is not None and hold is not None
    assert row.task_id is None and "extraction failed" in row.error
    assert row.next_check_at - row.created_at == asynctasks.MAX_AGE


async def test_pending_row_write_failure_releases_the_hold_and_alerts(
    clients: AsyncClient, monkeypatch, replicate_platform,
):
    async def fail_persistence(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(task_app, "defer_submission", fail_persistence)
    response = await _submit(clients, monkeypatch, {
        "id": "prediction-untracked",
        "urls": {"get": "https://api.replicate.com/v1/predictions/untracked"},
    })
    assert response.status_code == 201
    assert response.headers["X-Treg-Cost-Micro"] == "0"
    call_id = response.headers["X-Treg-Call-Id"]
    async with session_maker() as db:
        assert await db.get(AsyncTaskRecord, call_id) is None
        assert await db.get(Hold, call_id) is None
        entry = (await db.execute(select(LedgerEntry).where(
            LedgerEntry.call_id == call_id, LedgerEntry.kind == "release"))).scalar_one()
    # treg's own failure is treg's cost: the whole reserve goes back, nothing is settled.
    assert entry.amount_micro == 3000
    assert entry.meta.get("reason") == "async_task_not_recorded"


@pytest.fixture
def minimax_platform(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_MINIMAX", "test-platform-token")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "minimax")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_a_2xx_that_fails_the_expect_rule_releases_and_is_not_deferred(
    clients: AsyncClient, monkeypatch, minimax_platform,
):
    """MiniMax answers HTTP 200 with the error in the envelope (live 2026-09-02: base_resp 2013,
    "model MiniMax-Hailuo-2.3-Fast does not support Text-to-Video mode"). No task exists, so
    nothing may be deferred and nothing may be charged."""
    async def fake_relay(*args, **kwargs):
        return _response(200, {"task_id": "", "base_resp": {
            "status_code": 2013, "status_msg": "invalid params"}})

    monkeypatch.setattr(call_service, "relay", fake_relay)
    response = await clients.post("/call/minimax.video-gen.from_text", json={
        "model": "MiniMax-Hailuo-2.3", "prompt": "A paper boat.", "duration": 6,
        "resolution": "768P"})
    assert response.status_code == 200
    assert response.headers["X-Treg-Cost-Micro"] == "0"
    call_id = response.headers["X-Treg-Call-Id"]
    async with session_maker() as db:
        assert await db.get(AsyncTaskRecord, call_id) is None
        assert await db.get(Hold, call_id) is None
        entries = {e.kind: e.amount_micro for e in (await db.execute(select(LedgerEntry).where(
            LedgerEntry.call_id == call_id))).scalars().all()}
    # A failed envelope is a per_success miss: settled at zero, the whole reserve given back.
    assert entries == {"reserve": -280000, "settle": 0}


async def _due_submission(clients, monkeypatch, document: dict) -> str:
    response = await _submit(clients, monkeypatch, {
        "id": "prediction-worker",
        "urls": {"get": "https://api.replicate.com/v1/predictions/worker"},
    })
    call_id = response.headers["X-Treg-Call-Id"]
    async with session_maker() as db:
        row = await db.get(AsyncTaskRecord, call_id)
        row.next_check_at = utcnow_naive() - timedelta(seconds=1)
        await db.commit()

    async def fake_poll(row, client):
        return 200, json.dumps(document).encode()

    monkeypatch.setattr(task_app, "_poll", fake_poll)
    return call_id


async def test_worker_settles_terminal_success(
    clients: AsyncClient, monkeypatch, replicate_platform,
):
    call_id = await _due_submission(clients, monkeypatch, {"status": "succeeded", "output": ["url"]})
    result = await task_app.settle_due()
    assert result.settled == 1
    async with session_maker() as db:
        row = await db.get(AsyncTaskRecord, call_id)
        hold = await db.get(Hold, call_id)
        entry = (await db.execute(select(LedgerEntry).where(
            LedgerEntry.call_id == call_id, LedgerEntry.kind == "settle"))).scalar_one()
    assert row.status == "settled" and row.settled_micro == 3000
    assert hold is None and entry.amount_micro == -3000
    async with session_maker() as db:
        key = (await db.execute(select(ArchiveKey).where(
            ArchiveKey.req_url == f"treg://asynctasks/{call_id}"))).scalar_one()
        snapshot = (await db.execute(select(ArchiveSnapshot).where(
            ArchiveSnapshot.key_id == key.id))).scalar_one()
        report = await reconcile.async_task_settlement(
            db, utcnow_naive() - timedelta(hours=1))
    assert json.loads(snapshot.body)["status"] == "succeeded"
    assert report["providers"] == [{
        "provider": "replicate", "successes": 1, "failures": 0,
        "settled_micro": 3000, "tasks": 1, "success_rate": 1.0,
        "settled_usd": 0.003,
    }]


async def test_worker_releases_terminal_failure(
    clients: AsyncClient, monkeypatch, replicate_platform,
):
    call_id = await _due_submission(clients, monkeypatch, {"status": "failed", "error": "rejected"})
    result = await task_app.settle_due()
    assert result.released == 1
    async with session_maker() as db:
        row = await db.get(AsyncTaskRecord, call_id)
        hold = await db.get(Hold, call_id)
    assert row.status == "released" and row.settled_micro == 0 and hold is None


async def test_worker_backs_off_unknown_status(
    clients: AsyncClient, monkeypatch, replicate_platform,
):
    call_id = await _due_submission(clients, monkeypatch, {"status": "provider_added_a_state"})
    before = utcnow_naive()
    result = await task_app.settle_due()
    async with session_maker() as db:
        row = await db.get(AsyncTaskRecord, call_id)
        hold = await db.get(Hold, call_id)
    assert result.backed_off == 1
    assert row.status == "pending" and row.next_check_at > before and hold is not None
    async with session_maker() as db:
        hold = await db.get(Hold, call_id)
        hold.created_at = utcnow_naive() - timedelta(seconds=ledger.hold_ttl_s() + 1)
        await db.commit()
        assert await ledger.reap_stale_holds(db, org_id=row.org_id) == 0
        assert await db.get(Hold, call_id) is not None


async def test_worker_timeout_releases_the_hold_and_flags_it_for_review(
    clients: AsyncClient, monkeypatch, replicate_platform, caplog,
):
    """An outcome nobody observed is the platform's cost, never the customer's."""
    call_id = await _due_submission(clients, monkeypatch, {"status": "processing"})
    async with session_maker() as db:
        row = await db.get(AsyncTaskRecord, call_id)
        row.created_at = utcnow_naive() - asynctasks.MAX_AGE - timedelta(seconds=1)
        row.next_check_at = utcnow_naive() - timedelta(seconds=1)
        await db.commit()
    with caplog.at_level("ERROR", logger="treg.asynctasks"):
        result = await task_app.settle_due()
    async with session_maker() as db:
        row = await db.get(AsyncTaskRecord, call_id)
        hold = await db.get(Hold, call_id)
        entry = (await db.execute(select(LedgerEntry).where(
            LedgerEntry.call_id == call_id, LedgerEntry.kind == "release"))).scalar_one()
    assert result.timed_out == 1
    assert row.status == "timed_out" and row.settled_micro == 0 and row.reserved_micro == 3000
    assert hold is None and entry.meta.get("reconcile_review") is True
    assert any("ASYNC TASK TIMED OUT" in rec.message for rec in caplog.records)
    async with session_maker() as db:
        report = await reconcile.async_task_settlement(
            db, utcnow_naive() - timedelta(hours=1))
    assert [item["call_id"] for item in report["absorbed_timeouts"]] == [call_id]
    assert report["absorbed_timeouts"][0]["reserved_micro"] == 3000


def test_basis_derivation_and_settlement_table_vs_usage():
    table_cost = {"table": [{"when": {"body.n": 2}, "value": 0.01}],
                  "fallback": {"value": 0.04}, "settle": "table"}
    table = settlement.derive_basis(
        table_cost, request={"body": {"n": 2}}, input_schema={}, unit_micro=1_000_000,
        terminal=True)
    assert table["amount"]["kind"] == "table"
    assert settlement.settle(table, {"terminal": {}}) == 10_000

    usage_cost = {"settle": "usage", "usage": {"path": "usage.cost", "unit": "usd"},
                  "fallback": {"value": 1.0}}
    usage = settlement.derive_basis(
        usage_cost, request={}, input_schema={}, unit_micro=1_000_000, terminal=True)
    assert usage["amount"]["kind"] == "usage"
    assert settlement.settle(usage, {"terminal": {"usage": {"cost": 0.125}}}) == 125_000

    request = settlement.request_evidence(
        [("id", "42"), ("count", "2")], b"{}", path_names={"id"})
    path_table = {"table": [{"when": {"pathParams.id": 42}, "value": 0.01,
                             "times": "queryParams.count"}],
                  "fallback": {"value": 0.10}, "settle": "table"}
    schema = {"pathParams": {"id": {"type": "integer"}},
              "queryParams": {"count": {"type": "integer"}}}
    basis = settlement.derive_basis(
        path_table, request=request, input_schema=schema, unit_micro=1_000_000, terminal=True)
    assert basis["reserve_micro"] == 20_000


def test_terminal_classification_coerces_status_values_and_treats_none_as_progress():
    descriptor = {
        "status": {
            "path": "task.status",
            "success": [2],
            "failure": ["3"],
        },
    }

    assert asynctasks.classify_terminal(descriptor, {"task": {"status": "2"}}) == "success"
    assert asynctasks.classify_terminal(descriptor, {"task": {"status": 3}}) == "failure"
    assert asynctasks.classify_terminal(descriptor, {"task": {"status": None}}) == "progress"
    assert asynctasks.classify_terminal(descriptor, {"task": {}}) == "progress"


async def _activity_row(clients: AsyncClient, call_id: str) -> dict:
    await audit.drain()
    await archive.drain()
    rows = (await clients.get("/calls")).json()
    return next(row for row in rows if row["call_ref"] == call_id)


async def test_activity_reports_task_state_and_artifact(
    clients: AsyncClient, monkeypatch, replicate_platform,
):
    """The audit row froze the reserve as the charge; the feed must show what actually happened."""
    call_id = await _due_submission(clients, monkeypatch, {
        "status": "succeeded", "output": ["https://replicate.delivery/out.webp"]})
    pending = await _activity_row(clients, call_id)
    assert pending["cost_charged_micro"] is None
    assert pending["async_task"]["status"] == "pending"
    assert pending["async_task"]["reserved_micro"] == 3000
    assert pending["async_task"]["result_url"] is None

    assert (await task_app.settle_due()).settled == 1
    settled = await _activity_row(clients, call_id)
    assert settled["cost_charged_micro"] == 3000
    task = settled["async_task"]
    assert task["status"] == "settled" and task["settled_micro"] == 3000
    assert task["result_url"] == "https://replicate.delivery/out.webp"
    assert task["completed_at"] is not None

    one = (await clients.get(f"/calls/{call_id}")).json()
    assert one["async_task"]["result_url"] == "https://replicate.delivery/out.webp"
    assert one["call"]["cost_charged_micro"] == 3000 and one["charged_micro"] == 3000


async def test_activity_reports_refund_after_failure(
    clients: AsyncClient, monkeypatch, replicate_platform,
):
    call_id = await _due_submission(clients, monkeypatch, {"status": "failed", "error": "nsfw"})
    assert (await task_app.settle_due()).released == 1
    row = await _activity_row(clients, call_id)
    assert row["cost_charged_micro"] == 0
    assert row["async_task"]["status"] == "released"
    assert row["async_task"]["result_url"] is None


def test_artifact_reads_both_result_modes():
    by_path = {"result": {"path": "task.content.url", "ttl_note": "time-limited"}}
    found = asynctasks.artifact(by_path, {"task": {"content": {"url": "https://x.invalid/v.mp4"}}})
    assert found["result_url"] == "https://x.invalid/v.mp4" and found["ttl_note"] == "time-limited"
    assert found["fetch"] is None
    by_fetch = {"result": {"fetch": "minimax.video-gen.result.retrieve",
                           "fetch_param": {"in": "queryParams", "name": "file_id",
                                           "value_from": "file_id"},
                           "ttl_note": "9h"}}
    found = asynctasks.artifact(by_fetch, {"status": "Success", "file_id": "f-1"})
    assert found["result_url"] is None
    assert found["fetch"] == {"endpoint": "minimax.video-gen.result.retrieve",
                              "name": "file_id", "value": "f-1"}
    assert asynctasks.artifact(by_fetch, {"status": "Success"})["fetch"] is None
    assert asynctasks.artifact({}, {"anything": 1})["result_url"] is None


async def test_query_parameter_poll_travels_as_query_items(monkeypatch):
    """MiniMax v1 polls `GET /v1/query/video_generation?task_id=…`. The relay builds the upstream
    query from `query_items` only, so the id must ride there (live 2026-09-02: appended to the URL it
    arrived empty and the provider answered 2013 "invalid params" on every tick)."""
    seen = {}

    async def fake_relay(request, url, tool, *args, **kwargs):
        seen["url"], seen["query"] = url, request.query_items

        async def stream():
            yield b'{"status": "Success"}'

        async def close():
            return None

        return UpstreamResponse(200, (), stream(), close)

    monkeypatch.setattr(task_app, "relay", fake_relay)
    row = AsyncTaskRecord(
        call_id="q-1", org_id=1, provider="minimax", endpoint_id="minimax.video-gen.from_text",
        task_id="437372532953204", reserved_micro=1, next_check_at=utcnow_naive(),
        descriptor={"poll": {"endpoint": "minimax.video-gen.task.status",
                             "param": {"in": "queryParams", "name": "task_id"}}})
    status, body = await task_app._poll(row, None)
    assert status == 200 and body == b'{"status": "Success"}'
    assert seen["url"] == "https://api.minimax.io/v1/query/video_generation"
    assert seen["query"] == (("task_id", "437372532953204"),)
