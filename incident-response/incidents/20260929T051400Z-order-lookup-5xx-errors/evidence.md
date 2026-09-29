Route: /api/orders/{order_id}
Window: 2026-09-29T04:59:00.016297+00:00 to 2026-09-29T05:14:00.016297+00:00

Requests by status code (last 15m):
- 200: 0.0
- 404: 0.0
- 500: 5.03

Order IDs seen failing in logs: express-1002

Error log lines:
- 2026-09-29T05:12:01.474247+00:00 order_id=express-1002 trace_id=b2d244ae8c9cbfcf846002b485bfa3ac: order lookup request
- 2026-09-29T05:12:01.468317+00:00 order_id=express-1002 trace_id=24ec74b463fc7993db32c8d664f18322: order lookup request
- 2026-09-29T05:12:01.462601+00:00 order_id=express-1002 trace_id=2ccb00da55538b593545f632254d92b0: order lookup request
- 2026-09-29T05:12:01.455867+00:00 order_id=express-1002 trace_id=ac646cf920f641ed5d4a10171cbd5094: order lookup request
- 2026-09-29T05:12:01.442944+00:00 order_id=express-1002 trace_id=108a2811a740ae4d1a4830e8e6e9b42f: order lookup request

Error trace IDs (Tempo): b2d244ae8c9cbfcf846002b485bfa3ac, 24ec74b463fc7993db32c8d664f18322, 2ccb00da55538b593545f632254d92b0, ac646cf920f641ed5d4a10171cbd5094, 108a2811a740ae4d1a4830e8e6e9b42f