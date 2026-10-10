# Observed payloads (fill during testnet)

---

## T6 segment 1 — live testnet bot run (2026-10-04 22:41:06–22:55:10Z)

Scope: `HL_ENV=testnet`, `HL_DRY_RUN=0`, default strategy ($15 quotes, $60 cap), 840 s
segment. Source: `logs/t6_seg1.log` (all addresses redacted at capture time by
`_redact`: first 6 + last 4 chars). Raw totals: place=320, cancel=310, cancel_oid=3,
bulk_cancel=1, orderUpdates=635, userFills=8 (2 snapshots + 6 live).

### 1. RESTING — place response + orderUpdates(open)
```
2026-10-04 22:41:12,504 INFO mmbot.gateway: place raw: {"status":"ok","response":{"type":"order","data":{"statuses":[{"resting":{"oid":61853059328,"cloid":"0x4c012ccba1bf4e3c91e895136aacced9"}}]}}}
2026-10-04 22:41:12,502 INFO mmbot.gateway: orderUpdates raw: {"channel":"orderUpdates","data":[{"order":{"coin":"BTC","side":"B","limitPx":"86340.0","sz":"0.00017","oid":61853059328,"timestamp":1791153672083,"origSz":"0.00017","cloid":"0x4c012ccba1bf4e3c91e895136aacced9"},"status":"open","statusTimestamp":1791153672083}]}
```
Parser check: `parse_place_response` reads `statuses[].resting.oid` ✓ present.

### 2. FILLED — userFills (live) + FILL log + flatten close
Snapshot (empty) at subscribe:
```
2026-10-04 22:41:11,141 INFO mmbot.gateway: userFills raw: {"channel":"userFills","data":{"isSnapshot":true,"user":"0x92ee...f3ba","fills":[]}}
```
First live delivery (2 fills):
```
2026-10-04 22:42:03,083 INFO mmbot.gateway: userFills raw: {"channel":"userFills","data":{"user":"0x92ee...f3ba","fills":[{"coin":"BTC","px":"86428.0","sz":"0.00017","side":"A","time":1791153722698,"startPosition":"0.0","dir":"Open Short","closedPnl":"0.0","hash":"0x02d5...6b3722f2428ba69dfea446f2fda3","oid":61853092170,"crossed":false,"fee":"0.002203","tid":613584490053278,"cloid":"0x45376c86c0804728a6abed4f52e789c2","feeToken":"USDC","twapId":null},{"coin":"BTC","px":"86461.0","sz":"0.00017","side":"A","time":1791153722698,"startPosition":"-0.00017","dir":"Open Short","closedPnl":"0.0","hash":"0x02d5...6b3722f2428ba69dfea446f2fda3","oid":61853108426,"crossed":false,"fee":"0.002204","tid":921362420570325,"cloid":"0xf2bcde8b70044619ae89d9f3d6982f19","feeToken":"USDC","twapId":null}]}}
```
Bot FILL lines (7 fills in segment, all position changes explained):
```
2026-10-04 22:42:03,084 INFO mmbot: FILL sell 0.000170 @ 86428.000000 pos=-0.000170
2026-10-04 22:42:03,085 INFO mmbot: FILL sell 0.000170 @ 86461.000000 pos=-0.000340
2026-10-04 22:47:16,999 INFO mmbot: FILL buy 0.000170 @ 86482.000000 pos=-0.000170
2026-10-04 22:47:16,999 INFO mmbot: FILL buy 0.000170 @ 86473.000000 pos=0.000000
2026-10-04 22:50:56,209 INFO mmbot: FILL buy 0.000170 @ 86315.000000 pos=0.000170
2026-10-04 22:51:57,906 INFO mmbot: FILL sell 0.000170 @ 86338.000000 pos=0.000000
2026-10-04 22:53:58,695 INFO mmbot: FILL sell 0.000130 @ 86425.000000 pos=-0.000130
```
Shutdown flatten close (real market order, filled):
```
2026-10-04 22:55:09,662 WARNING mmbot.gateway: flatten -> {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"filled": {"totalSz": "0.00013", "avgPx": "86428.6", "oid": 61853846315}}]}}}
```

### 3. CANCELED — cancel-by-cloid response + orderUpdates(canceled) + shutdown bulk cancel
```
2026-10-04 22:41:17,270 INFO mmbot.gateway: cancel raw: {"status":"ok","response":{"type":"cancel","data":{"statuses":["success"]}}}
2026-10-04 22:41:17,269 INFO mmbot.gateway: orderUpdates raw: {"channel":"orderUpdates","data":[{"order":{"coin":"BTC","side":"B","limitPx":"86340.0","sz":"0.00017","oid":61853059328,"timestamp":1791153672083,"origSz":"0.00017","cloid":"0x4c012ccba1bf4e3c91e895136aacced9"},"status":"canceled","statusTimestamp":1791153676908}]}
2026-10-04 22:55:07,508 INFO mmbot.gateway: bulk_cancel raw: {"status":"ok","response":{"type":"cancel","data":{"statuses":["success"]}}}
```

### 4. CROSSED REJECT (post-only) — raw response
```
2026-10-04 22:50:58,647 INFO mmbot.gateway: place raw: {"status":"ok","response":{"type":"order","data":{"statuses":[{"error":"Post only order would have immediately matched, bbo was 86299@86300. asset=3"}]}}}
```
Verdict **A-01 VERIFIED**: text contains "immediately matched" → matches
`CROSS_ERROR_MARKERS` entry "immediately match" (client.py:30). Note the response also
carries `asset=3`.

### 5. CANCEL ALREADY-GONE — raw response (organic, requote race)
```
2026-10-04 22:51:57,903 INFO mmbot.gateway: cancel raw: {"status":"ok","response":{"type":"cancel","data":{"statuses":[{"error":"Order was never placed, already canceled, or filled. asset=3"}]}}}
```
Verdict **A-03 VERIFIED**: text contains "never placed", "already canceled", "filled"
→ matches `GONE_MARKERS` (client.py:31).

### 6. MARGIN REJECT — raw response
PENDING — captured in a later segment (deliberate over-margin probe, not observed here).

### 7. MIN-NOTIONAL REJECT — raw response
PENDING — captured in a later segment (deliberate $5-order probe per BUILD_SPEC §3).

### Field-name verification vs parsers
- **A-04 VERIFIED** (`orderUpdates`): raw shows `data[].order.{coin,side,limitPx,sz,oid,
  timestamp,origSz,cloid}` + `data[].status` (observed values `open`,`canceled`) — every
  field `parse_order_updates` reads is present (extra fields ignored).
- **A-05 VERIFIED** (`userFills`): raw shows `data.{isSnapshot,user,fills[].{coin,px,sz,
  side,time,startPosition,dir,closedPnl,hash,oid,crossed,fee,tid,cloid,feeToken,twapId}}`
  — every field `parse_fills` reads is present (incl. `isSnapshot` on snapshot messages;
  second snapshot arrived after the segment's feed restart).

### Segment result
```
2026-10-04 22:41:11,692 INFO mmbot: startup: equity=999.00 position=0.000000, cleared 0 stale orders
2026-10-04 22:55:10,464 INFO mmbot: stopped. metrics={'book': 159, 'places': 320, 'stale_pulls': 289, 'cancels': 309, 'stale_pruned': 6, 'fills': 7, 'orphans_cancelled': 3, 'risk_rejects': 10, 'cross_rejects': 1, 'pos_drift': 4, 'feed_restarts': 1}
FLAT_CHECK result=FLAT
```
FLAT confirmed: `open_orders=0 position=0.000000` (`accountValue=0.0` here is the
margin-only field of this spot-mode account — expected, see RUNBOOK equity note; combined
equity was 999–1002 during the run). 0 Tracebacks; 1 ERROR = benign testnet WS `Expired`
(22:52:06, auto-recovered). No kill switches. No unexplained position change.

---

## T6 segment 2 — deliberate reject probes (2026-10-04 23:03:00–23:06:19Z)

`scripts/t6_probes.py` (testnet-only assert; Alo post-only orders placed 5% below mid so
they cannot match; cancelled by cloid ~1 s later; address never printed). Source:
`logs/t6_seg2_probes.log`. Every run ended `PROBE result=CLEAN` + `FLAT_CHECK result=FLAT`.

### Run 1 (tick-size accident — bonus capture)
```
SEG2 start 2026-10-04T23:03:00Z
PROBE min_notional place px=82109.9 sz=6e-05 notional=4.93
PROBE min_notional raw: {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"error": "Price must be divisible by tick size. asset=3"}]}}}
PROBE margin raw: {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"error": "Price must be divisible by tick size. asset=3"}]}}}
```
Both probes rejected on price `82109.9` (BTC tick = 1.0 whole dollars) → probe px floored
to int; payload kept as a third error-text case:
`"Price must be divisible by tick size. asset=3"`.

### Run 2 — min-notional REJECT + margin probe at $1.3k (ACCEPTED)
```
PROBE min_notional raw: {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"error": "Order must have minimum value of $10. asset=3"}]}}}
PROBE margin place px=82116.0 sz=0.01583 notional=1299.90
PROBE margin raw: {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 61854369419, "cloid": "0x52b874909ab14875b079942663294410"}}]}}}
PROBE cancel_min_notional raw: {"status": "ok", "response": {"type": "cancel", "data": {"statuses": [{"error": "Order was never placed, already canceled, or filled. asset=3"}]}}}
PROBE cancel_margin raw: {"status": "ok", "response": {"type": "cancel", "data": {"statuses": ["success"]}}}
PROBE open_orders=0
PROBE result=CLEAN
PROBE_EXIT=0
FLAT_CHECK open_orders=0 position=0.000000 accountValue=0.0
FLAT_CHECK result=FLAT
FLAT_EXIT=0
```
- **§6 MIN-NOTIONAL — captured:** `{"error": "Order must have minimum value of $10.
  asset=3"}` — text matches the bot constant exactly → **A-02 VERIFIED**.
- **§7 MARGIN at $1,299.90 notional (1.3x equity): ACCEPTED and rested**
  (`oid 61854369419`), then cancel `success`. Placement-time margin is NOT binding at this
  size (~40x leverage would only need ~$33 against the account's ~$999).

### Run 3 — margin probe at $41k (REJECT)
```
PROBE min_notional raw: {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"error": "Order must have minimum value of $10. asset=3"}]}}}
PROBE margin place px=82202.0 sz=0.49877 notional=40999.89
PROBE margin raw: {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"error": "Insufficient margin to place order. asset=3"}]}}}
PROBE cancel_min_notional raw: {"status": "ok", "response": {"type": "cancel", "data": {"statuses": [{"error": "Order was never placed, already canceled, or filled. asset=3"}]}}}
PROBE cancel_margin raw: {"status": "ok", "response": {"type": "cancel", "data": {"statuses": [{"error": "Order was never placed, already canceled, or filled. asset=3"}]}}}
PROBE open_orders=0
PROBE result=CLEAN
PROBE_EXIT=0
FLAT_CHECK open_orders=0 position=0.000000 accountValue=0.0
FLAT_CHECK result=FLAT
FLAT_EXIT=0
```
- **§7 MARGIN — captured:** `sz=0.49877` / `notional=40999.89` → `{"error": "Insufficient
  margin to place order. asset=3"}`. Placement-time margin IS enforced; threshold on this
  ~$999 account lies between $1.3k and $41k notional (leverage-scaled).
- Min-notional reject reproduced verbatim (2nd sample).

### Segment 2 result
`open_orders=0` and `position=0.000000` after every run (`accountValue=0.0` = margin-only
field of this spot-mode account, expected). No position ever opened; probe orders never
crossed the book (post-only, 5% away).

### T6 capture inventory (all six BUILD_SPEC §3 cases + 2 bonus)
| case | status | where |
|---|---|---|
| resting | captured | seg1 §1 |
| filled | captured (7 fills + flatten close) | seg1 §2 |
| canceled | captured (cancel/bulk/orderUpdates) | seg1 §3 |
| crossed reject | captured (A-01) | seg1 §4 |
| min-notional reject | captured (A-02) | §6 |
| margin reject | captured | §7 |
| bonus: already-gone cancel | captured (A-03) | seg1 §5 + §6/§7 cancels |
| bonus: tick-size reject | captured | §8 run1 |
