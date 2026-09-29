"""Incident responder for Order Tracker.

Receives Grafana alert webhooks at POST /alerts, collects evidence from
Prometheus/Loki/Tempo, and runs Claude Code in a sandboxed headless mode to
investigate. See README.md for how to run it and autonomy-policy.yaml for
what the agent may and may not do.

Run on the host (it needs this repo's checkout, Docker, and a logged-in
`claude` CLI):

    cd incident-response && uv run --frozen python responder.py
"""

import json
import logging
import os
import re
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from ipaddress import ip_address, ip_network
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request

import agent_runner
import evidence as evidence_module

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
INCIDENTS_DIR = Path(os.getenv("RESPONDER_INCIDENTS_DIR", str(HERE / "incidents")))
TASK_TEMPLATE = (HERE / "responder-task.md").read_text()
VERIFY_SCRIPT = HERE / "runbooks" / "verify-recovery.sh"
REDEPLOY_SCRIPT = HERE / "runbooks" / "redeploy.sh"

PORT = int(os.getenv("RESPONDER_PORT", "8001"))
# Loopback for local curl, plus Docker's default bridge gateway for Grafana's
# container reaching the host. Never 0.0.0.0: this service can launch an
# agent that edits code, so it must not be reachable from the LAN.
ALLOWED_NETWORKS = [
    ip_network(n.strip())
    for n in os.getenv(
        "RESPONDER_ALLOWED_NETWORKS", "127.0.0.0/8,172.17.0.0/16,10.215.24.0/24"
    ).split(",")
    if n.strip()
]
BIND_HOSTS = [h.strip() for h in os.getenv("RESPONDER_BIND_HOSTS", "127.0.0.1,172.17.0.1").split(",") if h.strip()]
COOLDOWN_SECONDS = int(os.getenv("RESPONDER_COOLDOWN_SECONDS", "300"))
MAX_PAYLOAD_BYTES = 256 * 1024
ANSWER_LABELS = {
    "INCIDENT": "incident",
    "ROOT_CAUSE": "root_cause",
    "FIX": "fix",
    "TESTS": "tests",
    "ACTION": "action",
    "SUMMARY": "summary",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("incident_response")


def client_allowed(host: str | None) -> bool:
    if host is None:
        return False
    try:
        addr = ip_address(host)
    except ValueError:
        return False
    return any(addr in net for net in ALLOWED_NETWORKS)


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48] or "alert"


def utc_now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def parse_answer(text: str, last_line: str, incident_id: str) -> dict:
    fields = {"incident": incident_id, "root_cause": "", "fix": "", "tests": "", "action": "unknown", "summary": ""}
    for line in text.splitlines():
        # Strip markdown fences/bullets/quoting: the agent may wrap the
        # answer block in ``` even though the prompt asked for plain text.
        clean = line.strip().strip("`").lstrip("-*> ").strip()
        for label, key in ANSWER_LABELS.items():
            prefix = f"{label}:"
            if clean.upper().startswith(prefix):
                fields[key] = clean[len(prefix):].strip()
    action_words = fields["action"].split()
    action = action_words[0].strip(".,").lower() if action_words else "unknown"
    fields["action"] = action if action in {"none", "fixed", "escalate"} else "unknown"
    # "The last line of its answer" means the final SUMMARY line, not
    # whatever literally comes last in the raw text (a stray ``` fence,
    # trailing whitespace, ...). Fall back to the raw last line only if no
    # SUMMARY field was found at all.
    fields["last_line"] = f"SUMMARY: {fields['summary']}" if fields["summary"] else last_line
    return fields


def verify_recovery(paths: list[str]) -> dict:
    result = subprocess.run(
        [str(VERIFY_SCRIPT), *paths], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60
    )
    return {"paths": paths, "exit_code": result.returncode, "output": result.stdout + result.stderr}


def redeploy() -> dict:
    # The agent is never allowed to touch Docker (Bash(docker:*) is denied).
    # Rebuilding the running container so its fix actually takes effect is
    # the system's job, not the model's -- done here, after the code change,
    # before we trust anything the agent said about being "fixed".
    result = subprocess.run(
        [str(REDEPLOY_SCRIPT)], cwd=REPO_ROOT, capture_output=True, text=True, timeout=180
    )
    return {"exit_code": result.returncode, "output": result.stdout + result.stderr}


@dataclass
class Incident:
    id: str
    dir: Path
    alert: dict
    record: dict = field(default_factory=dict)

    @classmethod
    def create(cls, alert: dict, payload: dict) -> "Incident":
        alertname = alert.get("labels", {}).get("alertname", "alert")
        incident_id = f"{utc_now_stamp()}-{slugify(alertname)}"
        incident_dir = INCIDENTS_DIR / incident_id
        incident_dir.mkdir(parents=True, exist_ok=False)
        record = {
            "id": incident_id,
            "status": "received",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "alert": alert,
            "timeline": [],
        }
        incident = cls(id=incident_id, dir=incident_dir, alert=alert, record=record)
        incident.write_json("alert.json", payload)
        incident.transition("received", f"{alert.get('status', 'firing')} alert {alertname}")
        return incident

    def write_json(self, name: str, data) -> None:
        # Write-then-rename: a concurrent GET /incidents/{id} (or this test
        # suite's polling) must never observe a half-written file.
        target = self.dir / name
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str) + "\n")
        tmp.replace(target)

    def transition(self, status: str, note: str) -> None:
        self.record["status"] = status
        self.record["timeline"].append(
            {"at": datetime.now(timezone.utc).isoformat(), "status": status, "note": note}
        )
        self.write_json("incident.json", self.record)
        logger.info("incident %s: %s (%s)", self.id, status, note)

    @property
    def is_test(self) -> bool:
        return str(self.alert.get("labels", {}).get("test", "")).lower() == "true"

    @property
    def dedup_key(self) -> str:
        return self.alert.get("labels", {}).get("alertname", "alert")


def handle_incident(incident: Incident) -> None:
    # Every firing alert reaches the agent, including test ones: the
    # decision to skip investigation for a test notification belongs in
    # responder-task.md (the prompt), which the agent reads and follows --
    # not a Python-level shortcut that would mean "the agent's answer" was
    # never actually produced by the agent.
    try:
        gathered = evidence_module.collect()
        incident.write_json("evidence.json", gathered.to_dict())
        evidence_md = evidence_module.render_markdown(gathered)
        (incident.dir / "evidence.md").write_text(evidence_md)
        incident.transition(
            "evidence_collected",
            f"{len(gathered.error_logs)} error logs, {len(gathered.error_trace_ids)} error traces, "
            f"failing order ids: {gathered.failing_order_ids or 'none'}",
        )

        prompt = TASK_TEMPLATE.format(
            incident_id=incident.id,
            alert_json=json.dumps(incident.alert, indent=2),
            evidence_markdown=evidence_md,
        )
        (incident.dir / "prompt.md").write_text(prompt)

        incident.transition("agent_running", "claude --restricted, sandboxed to app/ and tests/")
        result = agent_runner.run(prompt, REPO_ROOT, incident.dir / "transcript.jsonl")
        (incident.dir / "response.md").write_text(result["answer_text"] + "\n")
        incident.record["agent"] = {k: v for k, v in result.items() if k != "answer_text"}

        answer = parse_answer(result["answer_text"], result["last_line"], incident.id)
        incident.write_json("answer.json", answer)
        incident.transition("agent_done", f"action={answer['action']} exit={result['exit_code']}")

        failing_paths = [f"/api/orders/{oid}" for oid in gathered.failing_order_ids]
        if answer["action"] == "fixed" and failing_paths:
            incident.transition("redeploying", "rebuilding the app container with the agent's fix")
            deploy = redeploy()
            incident.write_json("redeploy.json", deploy)
            if deploy["exit_code"] != 0:
                incident.transition("escalated", "redeploy failed; the agent's fix was never rebuilt")
                return
            verification = verify_recovery(failing_paths)
            incident.write_json("verification.json", verification)
            if verification["exit_code"] == 0:
                incident.transition("recovered", "verify-recovery.sh passed; fix awaits human review before commit")
            else:
                incident.transition("escalated", "agent reported 'fixed' but verification still failed")
        elif answer["action"] == "none":
            incident.transition("closed", "agent found nothing to fix")
        else:
            incident.transition("escalated", f"agent action '{answer['action']}' needs a human")
    except Exception as exc:
        logger.exception("incident %s failed", incident.id)
        incident.transition("escalated", f"responder error: {type(exc).__name__}: {exc}")


class Dispatcher:
    """Serializes agent runs (they share one checkout) and drops repeats of
    an alert that is already being handled."""

    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="responder")
        self.lock = threading.Lock()
        self.last_seen: dict[str, float] = {}

    def submit(self, alert: dict, payload: dict) -> dict:
        key = alert.get("labels", {}).get("alertname", "alert")
        now = time.monotonic()
        with self.lock:
            seen = self.last_seen.get(key)
            if seen is not None and now - seen < COOLDOWN_SECONDS:
                return {"alert": key, "status": "duplicate"}
            self.last_seen[key] = now
        incident = Incident.create(alert, payload)
        self.executor.submit(handle_incident, incident)
        return {"alert": key, "status": "accepted", "incident_id": incident.id}


app = FastAPI(title="Order Tracker incident responder")
dispatcher = Dispatcher()


@app.get("/healthz")
def health():
    return {"status": "ok"}


@app.post("/alerts", status_code=202)
async def receive_alerts(request: Request):
    client_host = request.client.host if request.client else None
    if not client_allowed(client_host):
        logger.warning("rejected alert webhook from %s", client_host)
        raise HTTPException(403, "client not allowed")

    body = await request.body()
    if len(body) > MAX_PAYLOAD_BYTES:
        raise HTTPException(413, "alert payload too large")
    try:
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(400, "body must be JSON")
    alerts = payload.get("alerts") if isinstance(payload, dict) else None
    if not isinstance(alerts, list):
        raise HTTPException(422, "expected a Grafana webhook payload with an 'alerts' list")

    results = []
    for alert in alerts:
        if not isinstance(alert, dict):
            continue
        if alert.get("status", "firing") != "firing":
            results.append({"alert": alert.get("labels", {}).get("alertname"), "status": "ignored"})
            continue
        results.append(dispatcher.submit(alert, payload))
    return {"results": results}


@app.get("/incidents")
def list_incidents():
    records = []
    for path in sorted(INCIDENTS_DIR.glob("*/incident.json"), reverse=True):
        record = json.loads(path.read_text())
        records.append({"id": record["id"], "status": record["status"]})
    return records


@app.get("/incidents/{incident_id}")
def get_incident(incident_id: str):
    if not re.fullmatch(r"[A-Za-z0-9-]+", incident_id):
        raise HTTPException(404, "incident not found")
    path = INCIDENTS_DIR / incident_id / "incident.json"
    if not path.exists():
        raise HTTPException(404, "incident not found")
    return json.loads(path.read_text())


def _bind_socket(host: str, port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    return sock


def main() -> None:
    INCIDENTS_DIR.mkdir(parents=True, exist_ok=True)
    sockets = [_bind_socket(host, PORT) for host in BIND_HOSTS]
    logger.info("listening on %s, port %d", ", ".join(BIND_HOSTS), PORT)
    uvicorn.Server(uvicorn.Config(app, log_level="info")).run(sockets=sockets)


if __name__ == "__main__":
    main()
