"""Collect the evidence behind an Order Tracker alert.

Every query here is read-only and scoped to the order-lookup endpoint. A
failure in any one backend (Prometheus, Loki, Tempo) is recorded, not
raised, so a partial telemetry outage never blocks the other two.
"""

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
LOKI_URL = os.getenv("LOKI_URL", "http://localhost:3100")
TEMPO_URL = os.getenv("TEMPO_URL", "http://localhost:3200")
SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "order-tracker")
ROUTE = "/api/orders/{order_id}"
LOOKBACK_MINUTES = int(os.getenv("EVIDENCE_LOOKBACK_MINUTES", "15"))
MAX_LOGS = int(os.getenv("EVIDENCE_MAX_LOGS", "50"))
MAX_TRACES = int(os.getenv("EVIDENCE_MAX_TRACES", "5"))


@dataclass
class Evidence:
    window_start: str
    window_end: str
    requests_by_status: dict
    error_logs: list
    error_trace_ids: list
    failing_order_ids: list
    errors: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "route": ROUTE,
            "window": {"start": self.window_start, "end": self.window_end},
            "requests_by_status_last_window": self.requests_by_status,
            "error_logs": self.error_logs,
            "error_trace_ids": self.error_trace_ids,
            "failing_order_ids": self.failing_order_ids,
            "collection_errors": self.errors,
        }


def _safe(errors: list, source: str, func, *args):
    try:
        return func(*args)
    except (httpx.HTTPError, ValueError, KeyError) as error:
        errors.append(f"{source}: {type(error).__name__}: {error}")
        return None


def _query_prometheus(client: httpx.Client, end: datetime) -> dict:
    query = (
        "sum by (http_status_code) (increase("
        f'order_lookup_requests_total{{http_route="{ROUTE}"}}[{LOOKBACK_MINUTES}m]))'
    )
    response = client.get(
        f"{PROMETHEUS_URL}/api/v1/query", params={"query": query, "time": end.timestamp()}
    )
    response.raise_for_status()
    result = response.json()["data"]["result"]
    return {r["metric"].get("http_status_code", "?"): round(float(r["value"][1]), 2) for r in result}


def _query_loki_errors(client: httpx.Client, start: datetime, end: datetime) -> list:
    query = f'{{service_name="{SERVICE_NAME}"}} | http_status_code=~"5.."'
    response = client.get(
        f"{LOKI_URL}/loki/api/v1/query_range",
        params={
            "query": query,
            "start": int(start.timestamp() * 1e9),
            "end": int(end.timestamp() * 1e9),
            "limit": MAX_LOGS,
            "direction": "backward",
        },
    )
    response.raise_for_status()
    entries = []
    for stream in response.json()["data"]["result"]:
        for ts, line in stream["values"]:
            entries.append(
                {
                    "timestamp": datetime.fromtimestamp(int(ts) / 1e9, timezone.utc).isoformat(),
                    "line": line,
                    "order_id": stream["stream"].get("order_id"),
                    "trace_id": stream["stream"].get("trace_id"),
                }
            )
    return entries


def _query_tempo_error_traces(client: httpx.Client, start: datetime, end: datetime) -> list:
    response = client.get(
        f"{TEMPO_URL}/api/search",
        params={
            "q": f'{{ resource.service.name="{SERVICE_NAME}" && status=error }}',
            "start": int(start.timestamp()),
            "end": int(end.timestamp()),
            "limit": MAX_TRACES,
        },
    )
    response.raise_for_status()
    return [t["traceID"] for t in response.json().get("traces", [])]


def collect(now: datetime | None = None, client: httpx.Client | None = None) -> Evidence:
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(minutes=LOOKBACK_MINUTES)
    client = client or httpx.Client(timeout=10)
    errors: list = []

    requests_by_status = _safe(errors, "prometheus", _query_prometheus, client, now) or {}
    error_logs = _safe(errors, "loki", _query_loki_errors, client, start, now) or []
    error_trace_ids = _safe(errors, "tempo", _query_tempo_error_traces, client, start, now) or []
    failing_order_ids = sorted({e["order_id"] for e in error_logs if e.get("order_id")})

    return Evidence(
        window_start=start.isoformat(),
        window_end=now.isoformat(),
        requests_by_status=requests_by_status,
        error_logs=error_logs,
        error_trace_ids=error_trace_ids,
        failing_order_ids=failing_order_ids,
        errors=errors,
    )


def render_markdown(evidence: Evidence) -> str:
    lines = [
        f"Route: {ROUTE}",
        f"Window: {evidence.window_start} to {evidence.window_end}",
        "",
        f"Requests by status code (last {LOOKBACK_MINUTES}m):",
    ]
    if evidence.requests_by_status:
        lines += [f"- {status}: {count}" for status, count in sorted(evidence.requests_by_status.items())]
    else:
        lines.append("- (no data)")

    lines.append("")
    lines.append(f"Order IDs seen failing in logs: {', '.join(evidence.failing_order_ids) or '(none found)'}")

    lines.append("")
    lines.append("Error log lines:")
    if evidence.error_logs:
        for entry in evidence.error_logs[:20]:
            lines.append(
                f"- {entry['timestamp']} order_id={entry.get('order_id')} "
                f"trace_id={entry.get('trace_id')}: {entry['line']}"
            )
    else:
        lines.append("- (none)")

    lines.append("")
    lines.append(f"Error trace IDs (Tempo): {', '.join(evidence.error_trace_ids) or '(none)'}")

    if evidence.errors:
        lines.append("")
        lines.append("Evidence collection problems (non-fatal):")
        lines += [f"- {err}" for err in evidence.errors]

    return "\n".join(lines)
