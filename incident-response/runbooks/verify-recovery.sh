#!/usr/bin/env bash
# Request each given order-lookup path on the running app; fail if any of
# them answers 5xx or doesn't answer at all. Used both by the agent (to
# check its own fix) and by the responder itself (to check the agent's claim).
#
# Usage: verify-recovery.sh /api/orders/express-1002 [/api/orders/... ...]
set -euo pipefail

base_url="http://127.0.0.1:${ORDER_TRACKER_PORT:-8000}"
attempts="${VERIFY_ATTEMPTS:-3}"

if [ "$#" -eq 0 ]; then
  echo "usage: $0 <path> [<path> ...]" >&2
  exit 2
fi

failed=0
for path in "$@"; do
  # Only order-lookup API paths -- no other hosts, schemes, or traversal.
  if [[ ! "$path" =~ ^/api/orders/[A-Za-z0-9_-]+$ ]]; then
    echo "REJECTED $path (only /api/orders/<id> paths are allowed)"
    failed=1
    continue
  fi
  for attempt in $(seq 1 "$attempts"); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$base_url$path" || true)"
    echo "attempt $attempt GET $path -> $code"
    if [[ "$code" == 000 || "$code" == 5* ]]; then
      failed=1
    fi
  done
done

if [ "$failed" -ne 0 ]; then
  echo "NOT RECOVERED"
  exit 1
fi
echo "RECOVERED"
