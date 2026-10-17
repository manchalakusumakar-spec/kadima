import asyncio
from datetime import datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace
import time

from cryptography.hazmat.primitives.asymmetric import rsa
import jwt
import pytest
from starlette.testclient import TestClient

from server_connector import collector
from server_connector.diagnostics import Evidence
from server_connector.server import Verifier, build, validate_config

NOW = datetime.fromisoformat("2026-10-06T19:44:06+05:30")
CFG = {"resource_url": "https://kadima.test/mcp", "issuer_url": "https://auth.test/",
       "jwks_url": "https://auth.test/keys", "allowed_subjects": ["operator-1"],
       "allowed_client_ids": ["chatgpt-1"]}


@pytest.fixture
def snapshot(tmp_path):
    path = tmp_path / "snapshot.json"
    data = {"schema": 1, "observed_at": NOW.isoformat(), "api": {
        "top20": {"ok": True, "data": {"ts": NOW.isoformat(), "config": {
            "kill_latched": False, "auto_trade_enabled": True, "rollout_enabled": True,
            "broker_orders_enabled": True}, "system": {}}},
        "desk": {"ok": True, "data": {"positions": [], "entry_diagnostics": [],
            "execution_progress": [{"signal_id": 1, "filled": 65, "quantity": 65, "state": "terminal"}]}}},
        "process_flags": {"KADIMA_CAS_LIVE": True, "KADIMA_CAS_EXECUTION": False},
        "audit": {"ok": True, "rows": []}}
    path.write_text(json.dumps(data))
    return path, data


def save(pair):
    path, data = pair
    path.write_text(json.dumps(data))
    return Evidence(path, lambda: NOW)


def test_green_flags_never_equal_ready(snapshot):
    e = save(snapshot)
    assert e.readiness("cas")["readiness"] == "UNVERIFIED"
    assert e.readiness("cas_legacy")["readiness"] == "BLOCKED"


@pytest.mark.parametrize("stamp", [None, "invalid", "2026-10-06T19:44:06",
    (NOW - timedelta(seconds=91)).isoformat(), (NOW + timedelta(seconds=2)).isoformat()])
def test_stale_and_missing_timestamps(snapshot, stamp):
    snapshot[1]["observed_at"] = stamp
    assert save(snapshot).readiness("cas")["readiness"] == "BLOCKED"


def test_missing_snapshot(tmp_path):
    assert Evidence(tmp_path / "missing").readiness("cas")["snapshot_status"] == "unavailable"


def test_no_symbol_or_time_based_cas_attribution(snapshot):
    snapshot[1]["audit"]["rows"] = [{"signal_id": 1, "symbol": "NIFTY06OCT2622700CE",
        "ts": "2026-10-06T15:20:00+05:30", "reason": "lane_permit_not_granted"}]
    e = save(snapshot)
    assert e.execution("cas")["ledger_rows"] == []
    assert e.execution("all")["ledger_rows"][0]["attributed_kind"] is None


def test_separate_legacy_and_new_cas(snapshot):
    snapshot[1]["audit"]["rows"] = [{"signal_id": 1, "kind": "cas_blast"}]
    e = save(snapshot)
    assert e.execution("cas")["ledger_rows"] == []
    assert e.execution("cas_legacy")["ledger_rows"][0]["filled"] == 65


def test_conflicting_attribution_is_unknown(snapshot):
    snapshot[1]["audit"]["rows"] = [{"signal_id": 1, "kind": "cas_blast"}, {"signal_id": 1, "kind": "cas"}]
    e = save(snapshot)
    assert e.execution("cas")["ledger_rows"] == []
    assert e.execution("all")["ledger_rows"][0]["attributed_kind"] is None


def test_sanitizes_credentials_and_unrecognised_payloads():
    raw = {"reason": "failure api_token=secret-value Bearer abc.def.ghi", "payload": {"password": "secret"},
           "api_token": "secret", "status": "open", "raw": {"jwtToken": "secret"}}
    value = collector.clean(raw)
    assert "secret" not in json.dumps(value)
    assert set(value) == {"reason", "status"}


def test_news_category_failures_survive_projection():
    value = collector.transform_api("top20", {"system": {"news": {"ok": False,
        "errors": {"company_mapping": "unresolved_symbols", "password": "secret"}}}})
    assert value["system"]["news"]["errors"] == {"company_mapping": "unresolved_symbols"}


def test_fixed_get_only_endpoints():
    with pytest.raises(ValueError):
        collector.get_local("/order_book", "not-a-real-token")
    with pytest.raises(ValueError):
        collector.get_local("https://attacker.test", "not-a-real-token")
    assert collector.NoRedirect().redirect_request(None, None, None, None, None, None) is None


def test_atomic_snapshot_permissions(snapshot):
    collector.write_snapshot(snapshot[1], snapshot[0])
    assert snapshot[0].stat().st_mode & 0o777 == 0o640
    assert json.loads(snapshot[0].read_text())["schema"] == 1


def test_untrusted_source_not_imported(monkeypatch, tmp_path):
    monkeypatch.setattr(collector, "REPO", tmp_path)
    monkeypatch.setattr(collector, "command", lambda args: "testcommit")
    (tmp_path / "kadima").mkdir()
    (tmp_path / "kadima/cas_config.py").write_text(
        "raise RuntimeError('do not import')\nCAS_API_TOKEN='secret'\nCAS_PRODUCT_TYPE='CARRYFORWARD'\n")
    source = collector.disk_source()["files"]["kadima/cas_config.py"]
    assert source["literal_policy_on_disk"] == {"CAS_PRODUCT_TYPE": "CARRYFORWARD"}


@pytest.fixture
def auth():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    class Keys:
        def get_signing_key_from_jwt(self, token):
            return SimpleNamespace(key=private.public_key())
    verifier = Verifier(CFG, Keys())
    def token(**changes):
        claims = {"sub": "operator-1", "client_id": "chatgpt-1", "iss": CFG["issuer_url"],
                  "aud": CFG["resource_url"], "iat": int(time.time()) - 1,
                  "exp": int(time.time()) + 60, "scope": "kadima:read", **changes}
        return jwt.encode(claims, private, algorithm="RS256")
    return verifier, token


@pytest.mark.parametrize("change", [{"sub": "other"}, {"aud": "https://wrong.test/mcp"},
    {"iss": "https://wrong.test/"}, {"client_id": "other"}, {"scope": "kadima:write"},
    {"exp": 1}, {"token_use": "id"}, {"iat": int(time.time()) + 9999}])
def test_oauth_fail_closed(auth, change):
    verifier, token = auth
    assert asyncio.run(verifier.verify_token(token(**change))) is None


def test_valid_token_and_bad_signature(auth):
    verifier, token = auth
    assert asyncio.run(verifier.verify_token(token())).subject == "operator-1"
    assert asyncio.run(verifier.verify_token(token() + "bad")) is None


def test_http_auth_discovery_and_actual_tool_call(auth, snapshot):
    verifier, token = auth
    app = build(CFG, save(snapshot), verifier)
    with TestClient(app, base_url="https://kadima.test") as client:
        headers = {"Accept": "application/json, text/event-stream"}
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        response = client.post("/mcp", json=request, headers=headers)
        assert response.status_code == 401
        assert "resource_metadata=" in response.headers["www-authenticate"]
        metadata = client.get("/.well-known/oauth-protected-resource/mcp").json()
        assert metadata["resource"] == CFG["resource_url"]
        headers["Authorization"] = "Bearer " + token()
        response = client.post("/mcp", json=request, headers=headers)
        assert response.status_code == 200, response.text
        tools = response.json()["result"]["tools"]
        assert {t["name"] for t in tools} == {"get_server_status", "get_strategy_readiness",
            "get_execution_evidence", "get_recent_diagnostics", "list_audit_days", "search_audit",
            "summarize_rejections"}
        assert all(t["annotations"]["readOnlyHint"] for t in tools)
        hostile_headers = dict(headers, Origin="https://attacker.test")
        assert client.post("/mcp", json=request, headers=hostile_headers).status_code == 403
        request.update(method="tools/call", params={"name": "get_strategy_readiness", "arguments": {"strategy": "cas"}})
        result = client.post("/mcp", json=request, headers=headers).json()["result"]
        assert result["structuredContent"]["readiness"] == "UNVERIFIED"
        request["params"] = {"name": "place_order", "arguments": {}}
        assert client.post("/mcp", json=request, headers=headers).json()["result"]["isError"]


@pytest.mark.parametrize("key,value", [("issuer_url", "http://auth.test/"),
    ("resource_url", "https://mcp.example.com/mcp"), ("allowed_subjects", []),
    ("allowed_client_ids", ["*"])])
def test_invalid_config(key, value):
    with pytest.raises(ValueError):
        validate_config(dict(CFG, **{key: value}))


def test_limits_and_unknown_filters(snapshot):
    e = save(snapshot)
    for value in (0, 51, True):
        with pytest.raises(ValueError):
            e.execution(limit=value)
    with pytest.raises(ValueError):
        e.readiness("unrecognised")


def test_failed_read_replaces_success(monkeypatch):
    monkeypatch.setattr(collector, "service_state", lambda: {})
    monkeypatch.setattr(collector, "disk_source", lambda: {})
    monkeypatch.setattr(collector, "audit_tail", lambda now: {"ok": False, "rows": []})
    calls = []
    def failed(name, token):
        calls.append(name)
        return {"ok": False, "error": "http_401"}
    monkeypatch.setattr(collector, "get_local", failed)
    snapshot = collector.collect("test-only-credential")
    assert set(calls) == set(collector.ROUTES)
    assert all(v == {"ok": False, "error": "http_401"} for v in snapshot["api"].values())
    assert "test-only-credential" not in json.dumps(snapshot)


def test_installer_apply_rejects_missing_auth_before_changes(monkeypatch, tmp_path):
    from server_connector import install
    monkeypatch.setattr(install.sys, "argv", ["install.py", "--apply"])
    monkeypatch.setattr(install.os, "geteuid", lambda: 0)
    monkeypatch.setattr(install, "ROOT", tmp_path / "never-created")
    def missing():
        raise ValueError("no_auth")
    monkeypatch.setattr(install, "config_check", missing)
    with pytest.raises(ValueError):
        install.main()
    assert not install.ROOT.exists()


def test_cognito_read_scope_requires_exact_scope(auth, snapshot):
    previous, token = auth
    cfg = dict(CFG, read_scope="kadima/read")
    verifier = Verifier(cfg, previous.keys)
    assert asyncio.run(verifier.verify_token(token())) is None
    access = token(scope="openid kadima/read", token_use="access")
    assert asyncio.run(verifier.verify_token(access)).subject == "operator-1"
    with TestClient(build(cfg, save(snapshot), verifier), base_url="https://kadima.test") as client:
        metadata = client.get("/.well-known/oauth-protected-resource/mcp").json()
        assert metadata["scopes_supported"] == ["kadima/read"]
        result = client.post("/mcp", headers={"Accept": "application/json, text/event-stream",
            "Authorization": "Bearer " + access}, json={"jsonrpc": "2.0", "id": 1,
            "method": "tools/list", "params": {}})
        assert result.status_code == 200


@pytest.mark.parametrize("scope", ["openid", "kadima:write", "kadima/read openid", "*", ""])
def test_scope_configuration_cannot_be_weakened(scope):
    with pytest.raises(ValueError):
        validate_config(dict(CFG, read_scope=scope))


def test_cognito_template_boundaries():
    template = json.loads((Path(__file__).parent / "cognito-template.json").read_text())
    resources = template["Resources"]
    assert all(r["Type"].startswith("AWS::Cognito::") for r in resources.values())
    pool = resources["OperatorPool"]["Properties"]
    assert pool["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"] is True
    assert pool["MfaConfiguration"] == "ON"
    assert pool["EnabledMfas"] == ["SOFTWARE_TOKEN_MFA"]
    client = resources["ChatGPTClient"]["Properties"]
    assert client["AllowedOAuthFlows"] == ["code"]
    assert client["GenerateSecret"] is False
    assert client["AllowedOAuthScopes"] == ["openid", "kadima/read"]
    # ChatGPT is optional: empty by default (never a guessed callback) and conditional.
    assert template["Parameters"]["ChatGPTRedirectUri"]["Default"] == ""
    assert resources["ChatGPTClient"]["Condition"] == "HasChatGPT"
    claude = resources["ClaudeClient"]["Properties"]
    assert claude["CallbackURLs"] == ["https://claude.ai/api/mcp/auth_callback"]
    assert claude["AllowedOAuthFlows"] == ["code"]
    assert claude["GenerateSecret"] is False
    assert claude["AllowedOAuthScopes"] == ["openid", "kadima/read"]


# ------------------------------------------------------------ audit history
from server_connector import archive


def _write(path, rows, tail=b""):
    with path.open("ab") as stream:
        for row in rows:
            stream.write(json.dumps(row).encode() + b"\n")
        stream.write(tail)


@pytest.fixture
def history(tmp_path, monkeypatch):
    source, target = tmp_path / "audit", tmp_path / "days"
    source.mkdir()
    monkeypatch.setattr(archive, "_disk_ok", lambda directory: True)
    return source, target


def test_archive_incremental_sanitized_copy(history):
    source, target = history
    day = source / "2026-10-06.jsonl"
    _write(day, [{"ts": "t1", "event": "cas_entry_rejected", "reason": "price_cap",
                  "symbol": "SENSEX15OCT2673300CE", "api_token": "secret-1",
                  "error": "Bearer abc.def.ghi failed", "raw": {"jwtToken": "secret-2"}},
                 {"ts": "t2", "event": "order_placed", "note": "password=secret-3"}],
           tail=b'{"ts":"t3","event":"half')  # record still being written
    first = archive.run(NOW, source, target)
    text = (target / "2026-10-06.jsonl").read_text()
    assert first["ok"] and text.count("\n") == 2
    assert "secret" not in text and "abc.def" not in text
    assert first["days"]["2026-10-06"]["caught_up"] is False  # partial tail pending
    with day.open("ab") as stream:
        stream.write(b'"}\n')
    second = archive.run(NOW, source, target)
    assert (target / "2026-10-06.jsonl").read_text().count("\n") == 3
    assert second["days"]["2026-10-06"]["caught_up"] is True
    archive.run(NOW, source, target)  # idempotent: nothing duplicated
    assert (target / "2026-10-06.jsonl").read_text().count("\n") == 3


def test_archive_skips_oversized_and_rebuilds_after_shrink(history, monkeypatch):
    source, target = history
    monkeypatch.setattr(archive, "MAX_LINE", 200)
    day = source / "2026-10-06.jsonl"
    day.write_bytes(b'{"event":"x","note":"' + b"a" * 500 + b'"}\n')
    _write(day, [{"event": "ok_event"}])
    result = archive.run(NOW, source, target)
    assert result["days"]["2026-10-06"]["skipped_records"] == 1
    assert "ok_event" in (target / "2026-10-06.jsonl").read_text()
    day.write_bytes(b'{"event":"fresh"}\n')  # replaced by a smaller file
    archive.run(NOW, source, target)
    assert (target / "2026-10-06.jsonl").read_text().strip() == '{"event":"fresh"}'


def test_archive_cap_budget_retention_and_low_disk(history, monkeypatch):
    source, target = history
    _write(source / "2026-10-06.jsonl", [{"event": "e", "n": i} for i in range(50)])
    _write(source / "2026-10-05.jsonl", [{"event": "old"}])
    target.mkdir()
    (target / "2026-09-01.jsonl").write_text("stale\n")
    monkeypatch.setattr(archive, "DAY_CAP", 200)
    result = archive.run(NOW, source, target)
    assert result["days"]["2026-10-06"]["capped"] is True
    assert "connector_archive_capped" in (target / "2026-10-06.jsonl").read_text()
    assert (target / "2026-10-05.jsonl").exists()  # backfilled with remaining budget
    assert not (target / "2026-09-01.jsonl").exists()  # outside retention
    monkeypatch.setattr(archive, "_disk_ok", lambda directory: False)
    _write(source / "2026-10-05.jsonl", [{"event": "later"}])
    assert archive.run(NOW, source, target)["paused"] == "low_disk"
    assert "later" not in (target / "2026-10-05.jsonl").read_text()


def test_archive_missing_source_changes_nothing(tmp_path):
    result = archive.run(NOW, tmp_path / "absent", tmp_path / "days")
    assert result["ok"] is False and not (tmp_path / "days").exists()


@pytest.fixture
def evidence_with_history(snapshot, tmp_path):
    days = tmp_path / "days"
    days.mkdir()
    _write(days / "2026-10-06.jsonl", [
        {"ts": "15:16", "event": "cas_entry_rejected", "reason": "price_cap", "symbol": "A"},
        {"ts": "15:17", "event": "cas_entry_rejected", "reason": "price_cap", "symbol": "B"},
        {"ts": "15:18", "event": "cas_entry_rejected", "reason": "outside_band", "symbol": "A"},
        {"ts": "15:19", "event": "entry_gate", "blocked_reasons": ["kill_latched"]},
        {"ts": "15:20", "event": "order_placed", "symbol": "C", "kind": "cas"}])
    snapshot[1]["archive"] = {"ok": True, "paused": None, "days": {"2026-10-06": {
        "caught_up": True, "capped": False, "archived_bytes": 500, "source_bytes": 900}}}
    path, data = snapshot
    path.write_text(json.dumps(data))
    return Evidence(path, lambda: NOW, archive_dir=days)


def test_rejection_summary_groups_by_event_and_reason(evidence_with_history):
    result = evidence_with_history.rejection_summary("2026-10-06", "cas_")
    assert result["complete"] is True
    top = result["refusal_groups"][0]
    assert (top["event"], top["reason"], top["count"]) == ("cas_entry_rejected", "price_cap", 2)
    assert top["sample_symbols"] == ["A", "B"] and top["kind"] is None
    all_events = evidence_with_history.rejection_summary("2026-10-06")
    assert all_events["blocked_reason_counts"] == [
        {"event": "entry_gate", "blocked_reason": "kill_latched", "count": 1}]


def test_search_audit_filters(evidence_with_history):
    e = evidence_with_history
    assert e.audit_search("2026-10-06", event="cas_*")["total_matches"] == 3
    assert e.audit_search("2026-10-06", contains="OUTSIDE")["total_matches"] == 1
    latest = e.audit_search("2026-10-06", limit=2)
    assert latest["total_matches"] == 5 and [r["ts"] for r in latest["matches"]] == ["15:19", "15:20"]
    assert e.audit_days()["days"][0]["complete"] is True


@pytest.mark.parametrize("day", ["../snapshot", "2026-10-6", "2026-02-30", "2026-09-01",
                                 "2026-10-07", 20261006, None])
def test_audit_history_rejects_bad_or_out_of_window_dates(evidence_with_history, day):
    with pytest.raises(ValueError):
        evidence_with_history.audit_search(day)


@pytest.mark.parametrize("kwargs", [{"event": "CAS.*"}, {"event": "a" * 70}, {"contains": "x" * 81},
                                    {"limit": 0}, {"limit": 101}, {"limit": True}])
def test_audit_search_rejects_bad_filters(evidence_with_history, kwargs):
    with pytest.raises(ValueError):
        evidence_with_history.audit_search("2026-10-06", **kwargs)


def test_unarchived_day_reports_not_archived(evidence_with_history):
    result = evidence_with_history.audit_search("2026-10-05")
    assert result["day_status"] == "not_archived" and result["total_matches"] == 0


def test_history_tool_over_http(auth, evidence_with_history):
    verifier, token = auth
    app = build(CFG, evidence_with_history, verifier)
    with TestClient(app, base_url="https://kadima.test") as client:
        headers = {"Accept": "application/json, text/event-stream", "Authorization": "Bearer " + token()}
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "summarize_rejections", "arguments": {"date": "2026-10-06", "event_prefix": "cas_"}}}
        result = client.post("/mcp", json=request, headers=headers).json()["result"]
        assert result["structuredContent"]["refusal_groups"][0]["count"] == 2
        request["params"]["arguments"] = {"date": "../../etc/passwd"}
        assert client.post("/mcp", json=request, headers=headers).json()["result"]["isError"]


def test_collector_archive_failure_does_not_block_snapshot(monkeypatch):
    monkeypatch.setattr(collector, "service_state", lambda: {})
    monkeypatch.setattr(collector, "disk_source", lambda: {})
    monkeypatch.setattr(collector, "audit_tail", lambda now: {"ok": True, "rows": []})
    monkeypatch.setattr(collector, "get_local", lambda name, token: {"ok": False, "error": "x"})
    def broken(now):
        raise RuntimeError("disk exploded")
    monkeypatch.setattr(archive, "run", broken)
    snapshot = collector.collect("t")
    assert snapshot["archive"] == {"ok": False, "error": "RuntimeError"}
    assert "completed_at" in snapshot


def test_archive_budget_is_small_during_market_hours():
    trading = datetime.fromisoformat("2026-10-15T11:00:00+05:30")
    evening = datetime.fromisoformat("2026-10-15T19:00:00+05:30")
    saturday = datetime.fromisoformat("2026-10-17T11:00:00+05:30")
    assert archive.run_budget(trading) == archive.MARKET_BUDGET < archive.run_budget(evening)
    assert archive.run_budget(saturday) == archive.OFF_HOURS_BUDGET
