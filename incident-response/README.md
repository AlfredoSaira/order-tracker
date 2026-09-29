# Incident Response

Receives Grafana alerts at `POST /alerts` on port `8001`. For each new
firing alert:

1. Saves the alert, then collects read-only evidence for the order-lookup
   endpoint from Prometheus, Loki, and Tempo (`evidence.py`).
2. Runs Claude Code in headless mode, sandboxed to `app/` and `tests/`, with
   a fixed allowlist of commands (`agent_runner.py`, policy documented in
   `autonomy-policy.yaml`).
3. If the agent claims it fixed the bug, the responder re-checks the
   endpoint itself (`runbooks/verify-recovery.sh`) before calling the
   incident "recovered" — the agent's own claim is never taken on faith.

See `responder-task.md` for the exact prompt the agent receives.

## Run it

This runs on the host, not inside Docker Compose: it needs this repo's
checkout, the Docker CLI (for `runbooks/redeploy.sh`), and a `claude` CLI
that is already logged in.

```bash
cd incident-response
uv run --frozen python responder.py
```

By default it only listens on `127.0.0.1` and Docker's default bridge
gateway (`172.17.0.1`) — never on all interfaces, since it can launch an
agent with write access to the app's code.

## Test it without a real alert

```bash
curl -X POST http://localhost:8001/alerts \
  -H 'Content-Type: application/json' \
  -d '{"alerts":[{"status":"firing","labels":{"alertname":"ResponderTest","test":"true"},"annotations":{"summary":"Test notification; no incident to fix"}}]}'
```

A `test` label short-circuits the flow: no evidence is collected and the
agent never runs. Check the result:

```bash
curl -s http://localhost:8001/incidents | python3 -m json.tool
curl -s http://localhost:8001/incidents/<incident_id> | python3 -m json.tool
cat incidents/<incident_id>/answer.json
```

## Connect it to Grafana

Point Grafana's contact point for the `order-lookup-5xx` alert rule at
`http://host.docker.internal:8001/alerts` (Grafana runs in a container;
this responder runs on the host).

## Incident folder

Each incident gets `incident-response/incidents/<id>/`:

| File | Contents |
| --- | --- |
| `alert.json` | The raw Grafana webhook payload |
| `evidence.json`, `evidence.md` | What was queried from Prometheus/Loki/Tempo |
| `prompt.md` | The exact prompt sent to the agent |
| `transcript.jsonl` | The agent's full `stream-json` output |
| `response.md` | The agent's raw final answer text |
| `answer.json` | That answer parsed into fields (see `response.schema.json`) |
| `verification.json` | The responder's own re-check, if the agent claimed "fixed" |
| `incident.json` | Status and timeline for the whole incident |

## Tests

```bash
uv run --frozen pytest -q
```

Prometheus/Loki/Tempo and the `claude` CLI are replaced with fakes — the
tests check the responder's own logic (dedup, test-alert short-circuit,
self-verification, escalation), not the real backends or a real agent run.
