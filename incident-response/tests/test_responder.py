import json
import time

import pytest
from fastapi.testclient import TestClient

import responder
from evidence import Evidence

FAKE_RESULT_BASE = {
    "command": ["claude"],
    "exit_code": 0,
    "timed_out": False,
    "duration_s": 0.1,
    "model": "fake",
    "permission_mode": "dontAsk",
    "tools": [],
    "num_turns": 1,
    "cost_usd": 0.01,
    "is_error": False,
    "permission_denials": [],
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(responder, "INCIDENTS_DIR", tmp_path)
    monkeypatch.setattr(responder, "client_allowed", lambda host: True)
    responder.dispatcher = responder.Dispatcher()
    return TestClient(responder.app)


def wait_for_status(incidents_dir, incident_id, statuses, timeout=2.5):
    deadline = time.monotonic() + timeout
    record = None
    while time.monotonic() < deadline:
        path = incidents_dir / incident_id / "incident.json"
        if path.exists():
            try:
                record = json.loads(path.read_text())
            except json.JSONDecodeError:
                record = None
            else:
                if record["status"] in statuses:
                    return record
        time.sleep(0.02)
    return record


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_rejects_disallowed_client(tmp_path, monkeypatch):
    monkeypatch.setattr(responder, "INCIDENTS_DIR", tmp_path)
    monkeypatch.setattr(responder, "client_allowed", lambda host: False)
    disallowed_client = TestClient(responder.app)
    response = disallowed_client.post("/alerts", json={"alerts": []})
    assert response.status_code == 403


def test_test_alert_still_runs_the_agent_which_declines_to_act(client, monkeypatch):
    # The "don't touch anything, this is a test" decision belongs in the
    # prompt (responder-task.md), which the agent reads and follows -- the
    # responder itself must still collect evidence and run the agent for
    # every firing alert, test or not, so "the agent's answer" is real.
    monkeypatch.setattr(responder.evidence_module, "collect", lambda: Evidence("t0", "t1", {}, [], [], []))
    fake_result = {
        **FAKE_RESULT_BASE,
        "answer_text": "ACTION: none\nSUMMARY: Test notification received; no incident to fix.",
        "last_line": "SUMMARY: Test notification received; no incident to fix.",
    }
    monkeypatch.setattr(responder.agent_runner, "run", lambda *a, **k: fake_result)

    payload = {
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": "ResponderTest", "test": "true"},
                "annotations": {"summary": "test notification, no incident to fix"},
            }
        ]
    }
    response = client.post("/alerts", json=payload)
    assert response.status_code == 202
    incident_id = response.json()["results"][0]["incident_id"]

    record = wait_for_status(responder.INCIDENTS_DIR, incident_id, {"closed", "recovered", "escalated"})
    assert record["status"] == "closed"
    answer = json.loads((responder.INCIDENTS_DIR / incident_id / "answer.json").read_text())
    assert answer["action"] == "none"
    assert answer["last_line"] == "SUMMARY: Test notification received; no incident to fix."


def test_real_alert_runs_full_flow_and_self_verifies(client, monkeypatch):
    fake_evidence = Evidence(
        window_start="t0",
        window_end="t1",
        requests_by_status={"500": 3},
        error_logs=[{"timestamp": "t", "line": "boom", "order_id": "express-1002", "trace_id": "abc"}],
        error_trace_ids=["abc"],
        failing_order_ids=["express-1002"],
    )
    monkeypatch.setattr(responder.evidence_module, "collect", lambda: fake_evidence)

    answer_text = (
        "INCIDENT: x\n"
        "ROOT_CAUSE: bad date math on express delivery\n"
        "FIX: app/main.py\n"
        "TESTS: uv run --frozen pytest -q -> 4 passed\n"
        "ACTION: fixed\n"
        "SUMMARY: Fixed the express delivery date bug."
    )
    fake_result = {
        **FAKE_RESULT_BASE,
        "answer_text": answer_text,
        "last_line": "SUMMARY: Fixed the express delivery date bug.",
    }
    monkeypatch.setattr(responder.agent_runner, "run", lambda *a, **k: fake_result)
    monkeypatch.setattr(
        responder, "verify_recovery", lambda paths: {"paths": paths, "exit_code": 0, "output": "RECOVERED"}
    )

    payload = {"alerts": [{"status": "firing", "labels": {"alertname": "Order lookup 5xx errors"}, "annotations": {}}]}
    response = client.post("/alerts", json=payload)
    incident_id = response.json()["results"][0]["incident_id"]

    record = wait_for_status(responder.INCIDENTS_DIR, incident_id, {"recovered", "escalated"})
    assert record["status"] == "recovered"
    answer = json.loads((responder.INCIDENTS_DIR / incident_id / "answer.json").read_text())
    assert answer["last_line"] == "SUMMARY: Fixed the express delivery date bug."
    assert answer["action"] == "fixed"


def test_unverified_fix_is_escalated(client, monkeypatch):
    fake_evidence = Evidence("t0", "t1", {"500": 1}, [], [], ["express-1002"])
    monkeypatch.setattr(responder.evidence_module, "collect", lambda: fake_evidence)
    fake_result = {
        **FAKE_RESULT_BASE,
        "answer_text": "ACTION: fixed\nSUMMARY: claims fixed",
        "last_line": "SUMMARY: claims fixed",
    }
    monkeypatch.setattr(responder.agent_runner, "run", lambda *a, **k: fake_result)
    monkeypatch.setattr(
        responder, "verify_recovery", lambda paths: {"paths": paths, "exit_code": 1, "output": "NOT RECOVERED"}
    )

    payload = {"alerts": [{"status": "firing", "labels": {"alertname": "Order lookup 5xx errors"}, "annotations": {}}]}
    response = client.post("/alerts", json=payload)
    incident_id = response.json()["results"][0]["incident_id"]

    record = wait_for_status(responder.INCIDENTS_DIR, incident_id, {"recovered", "escalated"})
    assert record["status"] == "escalated"


def test_duplicate_firing_alert_is_deduped(client, monkeypatch):
    monkeypatch.setattr(responder.evidence_module, "collect", lambda: Evidence("t0", "t1", {}, [], [], []))
    monkeypatch.setattr(
        responder.agent_runner,
        "run",
        lambda *a, **k: {**FAKE_RESULT_BASE, "answer_text": "ACTION: none\nSUMMARY: ok", "last_line": "SUMMARY: ok"},
    )

    payload = {"alerts": [{"status": "firing", "labels": {"alertname": "Order lookup 5xx errors"}, "annotations": {}}]}
    first = client.post("/alerts", json=payload).json()["results"][0]
    second = client.post("/alerts", json=payload).json()["results"][0]
    assert first["status"] == "accepted"
    assert second["status"] == "duplicate"


def test_resolved_alert_is_ignored_not_treated_as_new_incident(client):
    payload = {"alerts": [{"status": "resolved", "labels": {"alertname": "Order lookup 5xx errors"}, "annotations": {}}]}
    response = client.post("/alerts", json=payload)
    assert response.json()["results"][0]["status"] == "ignored"
