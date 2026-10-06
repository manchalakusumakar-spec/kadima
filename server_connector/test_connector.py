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
        assert len(tools) == 4
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
