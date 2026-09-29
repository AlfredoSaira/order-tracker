#!/usr/bin/env bash
# Rebuild and restart only the app container from the current working tree.
# The telemetry stack (Collector/Prometheus/Loki/Tempo/Grafana) keeps
# running, so the recovery shows up on the same dashboard used to detect it.
set -euo pipefail

cd "$(dirname "$0")/../.."
docker compose up --build -d --wait --no-deps app
docker compose ps app
