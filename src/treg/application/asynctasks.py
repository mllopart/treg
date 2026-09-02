"""Orchestrate deferred settlement for metered asynchronous provider tasks."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import timedelta

import httpx
from sqlalchemy import select

from .. import archive, oauth_providers
from ..domain import asynctasks
from ..domain import money as ledger
from ..domain.catalog import store as catalog_store
from ..domain.money import settlement
from ..infra.db import session_maker
from ..infra.upstream.relay import relay
from ..models import AsyncTaskRecord, Hold, Tool
from ..timeutil import utcnow_naive
from .call.resolve import _host_of, _platform_bindings
from .call.types import UpstreamRequest


log = logging.getLogger("treg.asynctasks")
DEFAULT_LIMIT = 50
GLOBAL_CONCURRENCY = 8
PROVIDER_CONCURRENCY = 2


def _json_value(value: object) -> object:
    """Detach catalog objects from YAML scalar types before storing them in a JSON column."""
    return json.loads(json.dumps(value, default=lambda item: item.isoformat()))


async def defer_submission(mk, body: bytes, org_id: int) -> int:
    """Persist the pending task before allowing the request path to leave its hold open."""
    now = utcnow_naive()
    error = ""
    task_id = poll_url = None
    try:
        document = json.loads(body)
        extracted = asynctasks.extract_submission(mk.async_descriptor or {}, document)
        task_id, poll_url = extracted.task_id, extracted.poll_url
        due = now + timedelta(seconds=60)
    except (ValueError, UnicodeDecodeError, asynctasks.ExtractionError) as exc:
        error = f"submission extraction failed: {exc}"[:500]
        due = now + asynctasks.MAX_AGE
    async with session_maker() as db:
        hold = await db.get(Hold, mk.call_id)
        if hold is None:
            raise RuntimeError("async submission hold disappeared before persistence")
        db.add(AsyncTaskRecord(
            call_id=str(mk.call_id), org_id=org_id, provider=mk.provider,
            endpoint_id=mk.endpoint_id, task_id=task_id, poll_url=poll_url,
            reserved_micro=hold.amount_micro, descriptor=_json_value(mk.async_descriptor or {}),
            settlement_basis=_json_value(mk.settlement_basis),
            created_at=now, next_check_at=due, error=error,
        ))
        await db.commit()
    mk.call_id = None
    return int(hold.amount_micro)


async def views_for(org_id: int, call_ids: list[str]) -> dict[str, dict]:
    """The task's own account of each metered async call, keyed by call id, for activity displays.

    The audit row froze the reserve as "charged" at submission; this is where the display learns
    what actually happened (settled amount, refund, 24-hour fallback) and what the caller bought.
    Terminal JSON comes from the archive; a settled task whose recording was shed still reports its
    money truthfully, only without a link.
    """
    if not call_ids:
        return {}
    async with session_maker() as db:
        rows = (await db.execute(
            select(AsyncTaskRecord).where(
                AsyncTaskRecord.org_id == org_id,
                AsyncTaskRecord.call_id.in_(list(call_ids))))).scalars().all()
    if not rows:
        return {}
    documents = await archive.load_terminal_responses(
        [row.call_id for row in rows if row.status == asynctasks.SETTLED])
    views: dict[str, dict] = {}
    for row in rows:
        view = {
            "status": row.status, "task_id": row.task_id,
            "reserved_micro": row.reserved_micro, "settled_micro": row.settled_micro,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "completed_at": row.completed_at.isoformat() if row.completed_at else None,
            "error": row.error or None,
            "result_url": None, "fetch_command": None,
            "ttl_note": ((row.descriptor.get("result") or {}).get("ttl_note") or None),
        }
        body = documents.get(row.call_id)
        if body is not None:
            try:
                found = asynctasks.artifact(row.descriptor, json.loads(body))
            except ValueError:
                found = {}
            view["result_url"] = found.get("result_url")
            fetch = found.get("fetch")
            if fetch:
                view["fetch_command"] = (
                    f"treg call {fetch['endpoint']} -p {fetch['name']}={fetch['value']}")
        views[row.call_id] = view
    return views


@dataclass(frozen=True)
class TickResult:
    claimed: int = 0
    settled: int = 0
    released: int = 0
    backed_off: int = 0
    timed_out: int = 0


async def _claim_due(limit: int, now) -> list[str]:
    async with session_maker() as db:
        rows = (await db.execute(
            select(AsyncTaskRecord)
            .where(AsyncTaskRecord.status == asynctasks.PENDING,
                   AsyncTaskRecord.next_check_at <= now)
            .order_by(AsyncTaskRecord.next_check_at, AsyncTaskRecord.call_id)
            .with_for_update(skip_locked=True).limit(limit)
        )).scalars().all()
        for row in rows:
            row.attempts += 1
            row.next_check_at = now + timedelta(seconds=60)
        await db.commit()
        return [row.call_id for row in rows]


def _poll_target(row: AsyncTaskRecord) -> tuple[str, str, list[tuple[str, str]]]:
    poll = row.descriptor.get("poll") or {}
    if row.poll_url:
        return "GET", row.poll_url, []
    endpoint_id = poll.get("endpoint")
    endpoint = catalog_store.load().by_id.get(endpoint_id)
    if not endpoint:
        raise RuntimeError(f"poll endpoint {endpoint_id!r} is not catalogued")
    provider = oauth_providers.get(row.provider)
    if provider is None or not provider.base_url:
        raise RuntimeError(f"provider {row.provider!r} is not relayable")
    url = provider.base_url.rstrip("/") + "/" + endpoint["path"].lstrip("/")
    query: list[tuple[str, str]] = []
    param = poll.get("param") or {}
    if param:
        name, value = str(param.get("name") or ""), row.task_id
        marker = "{" + name + "}"
        if marker in url:
            url = url.replace(marker, str(value))
        else:
            query.append((name, str(value)))
    return str(endpoint.get("method") or "GET"), url, query


async def _poll(row: AsyncTaskRecord, client: httpx.AsyncClient) -> tuple[int, bytes]:
    provider = oauth_providers.get(row.provider)
    if provider is None:
        raise RuntimeError(f"provider {row.provider!r} is not registered")
    method, url, query = _poll_target(row)
    # The query travels as `query_items`: the relay composes the upstream URL from those (it
    # forwards a URL's own query string nowhere), so a query-parameter poll (MiniMax v1
    # `?task_id=`) appended to the URL reached the provider empty — "invalid params" until the
    # 24-hour deadline. Path-parameter polls never showed it. Live 2026-09-02.
    tool = Tool(org_id=row.org_id, name=row.endpoint_id, owner="treg-worker",
                base_url=provider.base_url, host=_host_of(provider.base_url),
                bindings=_platform_bindings(provider))

    async def empty():
        if False:
            yield b""

    request = UpstreamRequest(method=method, raw_headers=(), query_items=tuple(query),
                              body_stream=empty, has_body=False)
    response = await relay(request, url, tool, [], client, force_identity=True)
    try:
        chunks = [chunk async for chunk in response.body_stream]
        return response.status, b"".join(chunks)
    finally:
        await response.close()


async def _finish(call_id: str, outcome: str, document: object | None, now) -> str:
    async with session_maker() as db:
        row = await db.get(AsyncTaskRecord, call_id, with_for_update=True)
        if row is None or row.status != asynctasks.PENDING:
            return "noop"
        if outcome == "success":
            evidence = {"terminal": document}
            raw = settlement.settle(row.settlement_basis, evidence)
            unobserved = (row.settlement_basis["amount"]["kind"] == "usage"
                          and settlement.usage_evidence(row.settlement_basis, evidence) is None)
            if unobserved:
                row.error = "usage field missing from the terminal response; settled at the reserve"
                log.error("ASYNC USAGE UNOBSERVED: call %s on %s succeeded but %s carried no usage "
                          "figure; settled at the reserve — check the provider's response shape",
                          row.call_id, row.provider, row.endpoint_id)
            row.settled_micro = await ledger.settle_in_transaction(db, row.call_id, raw, meta={
                "provider": row.provider, "cost_source": row.settlement_basis["amount"]["kind"],
                "async_task": True, **({"reconcile_review": True} if unobserved else {}),
            })
            row.status = asynctasks.SETTLED
        elif outcome == "failure":
            await ledger.release_in_transaction(db, row.call_id, reason="async_task_failed",
                                                meta={"provider": row.provider, "async_task": True})
            row.settled_micro = 0
            row.status = asynctasks.RELEASED
        elif outcome == "timed_out":
            # No terminal state in 24 hours means treg does not know whether the caller got
            # anything. The platform absorbs that uncertainty: the hold goes back to the team in
            # full, the upstream charge (if any) is treg's, and the row is flagged for a human.
            # Charging the reserve here would bill a customer for an outcome nobody observed.
            await ledger.release_in_transaction(db, row.call_id, reason="async_task_timed_out",
                                                meta={"provider": row.provider, "async_task": True,
                                                      "reconcile_review": True})
            row.settled_micro = 0
            row.status = asynctasks.TIMED_OUT
            row.error = "terminal state not observed within 24 hours; hold released, platform absorbs"
            log.error("ASYNC TASK TIMED OUT: call %s on %s (%s) had no terminal state in 24h; "
                      "released %d micro-USD to the team, platform absorbs the upstream charge — "
                      "check whether the provider changed its status field",
                      row.call_id, row.provider, row.endpoint_id, row.reserved_micro)
        else:
            row.next_check_at = asynctasks.next_check(now, row.attempts)
            await db.commit()
            return "backed_off"
        row.completed_at = now
        await db.commit()
        return row.status


async def _process(call_id: str, client: httpx.AsyncClient) -> str:
    now = utcnow_naive()
    async with session_maker() as db:
        row = await db.get(AsyncTaskRecord, call_id)
        if row is None or row.status != asynctasks.PENDING:
            return "noop"
        if asynctasks.expired(row.created_at, now):
            return await _finish(call_id, "timed_out", None, now)
        if row.error:
            return "backed_off"
        snapshot = row.model_copy()
    try:
        status, body = await _poll(snapshot, client)
        if status >= 500:
            return await _finish(call_id, "progress", None, now)
        document = json.loads(body)
        outcome = asynctasks.classify_terminal(snapshot.descriptor, document)
        result = await _finish(call_id, outcome, document, now)
        if outcome in ("success", "failure"):
            await archive.store_terminal_response(
                snapshot.call_id, snapshot.provider, snapshot.endpoint_id, status, body)
        return result
    except (httpx.HTTPError, ValueError, UnicodeDecodeError, RuntimeError) as exc:
        log.warning("async poll failed for call %s: %s", call_id, exc)
        return await _finish(call_id, "progress", None, now)


async def settle_due(*, limit: int = DEFAULT_LIMIT, client: httpx.AsyncClient | None = None) -> TickResult:
    """Claim one worker tick and process network waits under global and provider caps."""
    now = utcnow_naive()
    call_ids = await _claim_due(limit, now)
    if not call_ids:
        return TickResult()
    global_sem = asyncio.Semaphore(GLOBAL_CONCURRENCY)
    provider_sems: dict[str, asyncio.Semaphore] = {}
    owned = client is None
    client = client or httpx.AsyncClient(timeout=60)

    async def run(call_id: str) -> str:
        async with session_maker() as db:
            row = await db.get(AsyncTaskRecord, call_id)
            provider = row.provider if row else ""
        sem = provider_sems.setdefault(provider, asyncio.Semaphore(PROVIDER_CONCURRENCY))
        async with global_sem, sem:
            return await _process(call_id, client)

    try:
        outcomes = await asyncio.gather(*(run(call_id) for call_id in call_ids))
    finally:
        if owned:
            await client.aclose()
    return TickResult(
        claimed=len(call_ids), settled=outcomes.count(asynctasks.SETTLED),
        released=outcomes.count(asynctasks.RELEASED),
        backed_off=outcomes.count("backed_off"), timed_out=outcomes.count(asynctasks.TIMED_OUT),
    )
