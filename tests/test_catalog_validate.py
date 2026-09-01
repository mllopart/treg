import pytest

from scripts import catalog_validate as validator


def test_cost_modifiers_accept_only_supported_declarative_credit_rules():
    base = {
        "type": "per_success", "value": 5, "currency": "credit", "per": 1,
        "unit": "call", "source": "docs", "source_url": "https://example.com/pricing",
        "checked": "2026-08-25", "confidence": "documented",
    }
    errors: list[str] = []
    validator.check_cost(base | {"settle": "modifiers", "modifiers": {
        "preview": {"location": "query", "when": "truthy", "set_credits": 0},
        "email": {"location": "lookups", "when": "present", "add_credits": 3,
                  "reserve_only": True},
        "enrich": {"location": "query", "when": "truthy", "add_credits_per_result": 1},
    }}, "catalog:test", errors, [])
    assert errors == []

    broken: list[str] = []
    validator.check_cost(base | {"modifiers": {
        "preview": {"location": "headers", "set_credits": 1},
        "email": {"add_credits": -1, "add_credits_per_result": 2},
        "rescrape": {"add_credits": 2, "reserve_only": "yes"},
        "enrich": {"add_credits_per_result": 1, "reserve_only": True},
    }}, "catalog:test", broken, [])
    assert any("location must be query, body, or lookups" in error for error in broken)
    assert any("set_credits currently supports only the free value 0" in error for error in broken)
    assert any("needs exactly one credit effect" in error for error in broken)
    assert any("add_credits must be a non-negative number" in error for error in broken)
    assert any("reserve_only must be a boolean" in error for error in broken)
    assert any("reserve_only currently supports only add_credits" in error for error in broken)

    bad_settle: list[str] = []
    validator.check_cost(base | {"settle": "estimate"}, "catalog:test", bad_settle, [])
    assert any("cost.settle currently supports only 'base' or 'modifiers'" in error for error in bad_settle)


def test_status_marker_references_must_exist_and_end_at_a_live_endpoint():
    statuses = {"provider.old": "retired", "provider.live": "", "provider.dead": "broken"}

    errors: list[str] = []
    validator.check_status_marker(
        {"id": "provider.old", "status": "retired", "status_note": "moved",
         "superseded_by": "provider.live"},
        "catalog:provider.old", statuses, errors,
    )
    assert errors == []

    broken: list[str] = []
    validator.check_status_marker(
        {"id": "provider.old", "status": "retired", "status_note": "",
         "superseded_by": "provider.missing"},
        "catalog:provider.old", statuses, broken,
    )
    validator.check_status_marker(
        {"id": "provider.old", "status": "retired", "status_note": "moved",
         "superseded_by": "provider.dead"},
        "catalog:provider.old", statuses, broken,
    )
    validator.check_status_marker(
        {"id": "provider.old", "status": "Retired", "status_note": "wrong spelling"},
        "catalog:provider.old", statuses, broken,
    )
    assert any("requires a non-empty status_note" in error for error in broken)
    assert any("is not a catalog endpoint id" in error for error in broken)
    assert any("is itself broken" in error for error in broken)
    assert any("status 'Retired' not one of" in error for error in broken)


def _valid_async():
    return {
        "id_from": "task_id",
        "poll": {"endpoint": "demo.video-gen.status", "param": {"pathParams": "task_id"}},
        "status": {"path": "task.status", "success": ["succeeded"],
                   "failure": ["failed", "cancelled"]},
        "result": {"path": "task.content.url", "ttl_note": "9h"},
        "interval": 10,
    }


def _async_errors(descriptor, cost=None):
    errors: list[str] = []
    validator.check_async_descriptor(
        descriptor, "demo.yaml:submit", "demo",
        {"demo.video-gen.status": "demo", "demo.video-gen.content": "demo",
         "other.video-gen.status": "other"},
        cost or {"type": "per_success"}, errors,
    )
    return errors


def test_async_descriptor_accepts_both_poll_and_result_modes():
    assert _async_errors(_valid_async()) == []
    dynamic = _valid_async()
    dynamic["poll"] = {"url_from": "urls.get", "url_hosts": ["api.example.com"]}
    dynamic["result"] = {
        "fetch": "demo.video-gen.content", "fetch_param": {"pathParams": "video_id"}}
    assert _async_errors(dynamic) == []


@pytest.mark.parametrize(("mutate", "message"), [
    (lambda d: d.update(id_from=""), "async.id_from must be non-empty"),
    (lambda d: d.update(poll=[]), "async.poll must be a mapping"),
    (lambda d: d.update(poll={}), "async.poll needs exactly one"),
    (lambda d: d.update(poll={"endpoint": "demo.video-gen.status", "url_from": "url",
                              "param": {"body": "id"}, "url_hosts": ["api.example.com"]}),
     "async.poll needs exactly one"),
    (lambda d: d.update(poll={"endpoint": "other.video-gen.status", "param": {"body": "id"}}),
     "existing same-provider catalog id"),
    (lambda d: d.update(poll={"endpoint": "demo.video-gen.status"}),
     "requires a single task-id parameter mapping"),
    (lambda d: d.update(poll={"endpoint": "demo.video-gen.status",
                              "param": {"headers": "task_id"}}),
     "must map pathParams, queryParams, or body"),
    (lambda d: d.update(poll={"endpoint": "demo.video-gen.status", "param": {"body": "id"},
                              "url_hosts": ["api.example.com"]}),
     "url_hosts is only valid with url_from"),
    (lambda d: d.update(poll={"url_from": "url"}), "requires non-empty url_hosts"),
    (lambda d: d.update(poll={"url_from": "url", "url_hosts": [""]}),
     "requires non-empty url_hosts"),
    (lambda d: d.update(poll={"url_from": "url", "url_hosts": ["https://api.example.com"]}),
     "requires non-empty url_hosts"),
    (lambda d: d.update(poll={"url_from": "url", "url_hosts": ["api.example.com"],
                              "param": {"body": "id"}}), "param is only valid with endpoint"),
    (lambda d: d.update(status=[]), "async.status must be a mapping"),
    (lambda d: d["status"].update(path=""), "async.status.path must be non-empty"),
    (lambda d: d["status"].update(success=[]), "async.status.success must be a non-empty list"),
    (lambda d: d["status"].update(failure=[]), "async.status.failure must be a non-empty list"),
    (lambda d: d["status"].update(failure=["succeeded"]), "must not overlap"),
    (lambda d: d["status"].update(success=[{"done": True}]),
     "values must be non-empty strings or numbers"),
    (lambda d: d.update(result=[]), "async.result must be a mapping"),
    (lambda d: d.update(result={}), "async.result needs exactly one"),
    (lambda d: d.update(result={"path": "url", "fetch": "demo.video-gen.content"}),
     "async.result needs exactly one"),
    (lambda d: d.update(result={"fetch": "other.video-gen.status",
                                "fetch_param": {"body": "id"}}),
     "existing same-provider catalog id"),
    (lambda d: d.update(result={"fetch": "demo.video-gen.content"}),
     "requires a single task-id parameter mapping"),
    (lambda d: d.update(result={"path": "url", "fetch_param": {"body": "id"}}),
     "fetch_param is only valid with fetch"),
    (lambda d: d.update(result={"path": "url", "ttl_note": ""}),
     "ttl_note must be non-empty"),
    (lambda d: d.update(interval=0), "async.interval must be a positive number"),
])
def test_async_descriptor_rejects_each_invalid_contract_shape(mutate, message):
    descriptor = _valid_async()
    mutate(descriptor)
    errors = _async_errors(descriptor)
    assert any(message in error for error in errors), errors


def test_async_descriptor_must_be_a_mapping():
    assert any("async must be a mapping" in error for error in _async_errors([]))


def test_async_descriptor_requires_per_success_cost():
    errors = _async_errors(_valid_async(), {"type": "per_call"})
    assert any("cost.type per_success" in error for error in errors)


def _valid_table():
    return {
        "type": "per_success",
        "table": [
            {"when": {"model": "Hailuo", "duration": 6}, "value": 0.3},
            {"when": {"model": "H3"}, "value": 0.13, "times": "duration"},
        ],
        "fallback": {"value": 2.0, "note": "most expensive supported combination"},
        "currency": "USD",
        "settle": "table",
        "source": "docs",
        "source_url": "https://example.com/pricing",
        "checked": "2026-09-01",
        "confidence": "documented",
    }


def _valid_input():
    return {"body": {
        "model": {"type": "string", "required": True},
        "duration": {"type": "integer", "required": False, "default": 6, "max": 10},
    }}


def _table_errors(cost, input_schema=None):
    errors: list[str] = []
    validator.check_cost(cost, "demo.yaml:submit", errors, [], input_schema or _valid_input())
    return errors


def test_cost_table_accepts_subset_rows_times_bounds_and_usage_settlement():
    assert _table_errors(_valid_table()) == []
    usage = _valid_table() | {
        "settle": "usage", "usage": {"path": "usage.cost", "unit": "usd"}}
    assert _table_errors(usage) == []


@pytest.mark.parametrize(("mutate", "message"), [
    (lambda c: c.update(table=[]), "cost.table must be a non-empty list"),
    (lambda c: c.update(table=["row"]), "table row must be a mapping"),
    (lambda c: c["table"][0].update(when={}), "when must be a non-empty mapping"),
    (lambda c: c["table"][0].update(when={"unknown": "x"}), "is not declared in input"),
    (lambda c: c["table"][0].update(value=-1), "value must be a non-negative number"),
    (lambda c: c["table"][1].update(times="frames"), "times field 'frames' is not declared"),
    (lambda c: c["table"][1].update(times=""), "times must name an input field"),
    (lambda c: c.pop("fallback"), "requires a fallback mapping"),
    (lambda c: c["fallback"].update(value=-1), "fallback.value must be a non-negative number"),
    (lambda c: c["fallback"].update(note=""), "fallback.note must explain"),
    (lambda c: c["fallback"].update(value=1.0), "must be at least every table row"),
    (lambda c: c.update(settle="later"), "settle must be 'table' or 'usage'"),
    (lambda c: c.update(settle="usage"), "requires usage.path and usage.unit"),
    (lambda c: c.update(usage={"path": "usage.cost", "unit": "usd"}),
     "usage is only valid with settle: usage"),
    (lambda c: c.update(currency="points"), "cost.table currency must be one of"),
])
def test_cost_table_rejects_each_invalid_contract_shape(mutate, message):
    cost = _valid_table()
    mutate(cost)
    errors = _table_errors(cost)
    assert any(message in error for error in errors), errors


def test_cost_table_when_fields_need_required_or_default_and_times_needs_max():
    optional = _valid_input()
    optional["body"]["duration"].pop("default")
    errors = _table_errors(_valid_table(), optional)
    assert any("must be required or declare a default" in error for error in errors)

    no_max = _valid_input()
    no_max["body"]["duration"].pop("max")
    errors = _table_errors(_valid_table(), no_max)
    assert any("must declare a positive input max" in error for error in errors)


def test_validator_checks_provider_async_defaults_after_endpoint_merge(tmp_path, monkeypatch, capsys):
    (tmp_path / "capabilities.yaml").write_text(
        "platforms: {video-gen: Video}\n"
        "capabilities: {video-gen.from_text: Generate}\n")
    (tmp_path / "fx.yaml").write_text("credit_rates_usd: {}\n")
    (tmp_path / "tikhub.yaml").write_text(
        "provider: tikhub\n"
        "source: {docs: https://example.com/docs}\n"
        "async:\n"
        "  id_from: task_id\n"
        "  poll: {url_from: urls.get, url_hosts: [api.example.com]}\n"
        "  status: {path: status, success: [done], failure: [failed]}\n"
        "  result: {path: output.url}\n"
        "  interval: 10\n"
        "endpoints:\n"
        "  - id: tikhub.video-gen.from-text\n"
        "    capability: video-gen.from_text\n"
        "    platform: video-gen\n"
        "    method: POST\n"
        "    path: /generate\n"
        "    summary: Generate a video\n"
        "    input:\n"
        "      body:\n"
        "        model: {type: string, required: true}\n"
        "        duration: {type: integer, required: false, default: 6, max: 10}\n"
        "    async: {status: {success: [succeeded]}}\n"
        "    cost:\n"
        "      type: per_success\n"
        "      table: [{when: {model: H3}, value: 0.13, times: duration}]\n"
        "      fallback: {value: 1.3, note: Maximum duration}\n"
        "      currency: USD\n"
        "      source: docs\n"
        "      source_url: https://example.com/pricing\n"
        "      checked: 2026-09-01\n"
        "      confidence: documented\n")
    monkeypatch.setattr(validator, "CATALOG", tmp_path)

    assert validator.main(["tikhub"]) == 0
    assert "0 error(s)" in capsys.readouterr().out
