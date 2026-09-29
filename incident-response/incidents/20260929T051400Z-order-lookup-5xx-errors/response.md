I fixed the bug behind the 500s on lookups of `express-1002`, and the test suite now passes (4 passed).

**What went wrong:** in `app/main.py:58`, `order_detail` estimates express delivery with `placed_at.replace(day=placed_at.day + 2)`. That doesn't roll over into the next month, so any express order placed in the last two days of a month raises `ValueError: day is out of range for month`, which comes back as a 500. The seeded order `express-1002` is always dated the last day of the previous month, so every lookup of it failed. That matches the evidence: 100% 500s, all for that one order ID.

**Fix:** that line now uses `placed_at + timedelta(days=2)`, which crosses month and year boundaries correctly. `timedelta` was already imported.

**Regression test:** I added `test_express_order_placed_at_month_end` to `tests/test_api.py`. It inserts an express order dated 2026-08-31 and expects a 200 with `estimated_delivery == "2026-09-02"`. To confirm it catches the bug, I briefly put the old line back: that test failed with the same `ValueError` while the other 3 passed. With the fix back in place, all 4 pass.

Running pytest also prints a `ValueError: I/O operation on closed file` traceback at the end. That comes from the OpenTelemetry console exporter writing after the test run closes its output, not from the fix, and it doesn't affect the results.

Nothing has been committed, pushed or restarted. The diff is ready for review.

INCIDENT: 20260929T051400Z-order-lookup-5xx-errors
ROOT_CAUSE: order_detail computed express delivery with placed_at.replace(day=placed_at.day + 2), which raises ValueError for orders placed in the last two days of a month (like the seeded express-1002), so those lookups returned 500.
FIX: app/main.py now computes the estimate with placed_at + timedelta(days=2); tests/test_api.py adds test_express_order_placed_at_month_end as a regression test.
TESTS: uv run --frozen pytest -q -> 4 passed (the new test fails without the fix with ValueError: day is out of range for month)
ACTION: fixed
SUMMARY: Order lookups for express orders placed at month end returned 500 because of a date overflow, and the fix switches to timedelta with a regression test added, ready for human review.
