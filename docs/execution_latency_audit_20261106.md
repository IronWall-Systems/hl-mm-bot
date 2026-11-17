# Execution-path latency audit — Tokyo node — 2026-11-06

Scope: `src/mmbot/` production path vs `scripts/measure_latency.py`, REST vs
WebSocket post, `batchModify` vs cancel+place, cancel by `oid` vs `cloid`,
network/edge routing, and kernel settings.

Safety: every live order was a HYPE perp `Alo` post-only BUY ~50% below mid
(~$11.5 notional). The only non-Alo case was the explicit `Gtc` comparison,
also 50% below mid. All orders were cancelled; final `open_orders=[]` and perp
`accountValue=$24.29537` (unchanged). No fills, no fees.

Artifacts:
- `scripts/benchmark_execution_paths.py` — benchmark harness
- `logs/execution_paths_benchmark.json` — 220 orders, raw records + transcript
- `logs/network_diagnostics_20261106.txt` — mtr / curl / sysctl / headers
- `/tmp/opencode/diag_cancel.py`, `/tmp/opencode/diag_dualmodify.py`,
  `/tmp/opencode/diag_bulk.py` — focused diagnostics

---

## 1. Codebase audit (`src/mmbot/`)

| Item | Finding |
|---|---|
| Transport | Sync Hyperliquid SDK `Exchange` (`requests.Session`) called via `asyncio.to_thread`; not `aiohttp`/`httpx`; no WS `post` |
| Serialization | Every action takes `self._lock`; `asyncio.gather` in `_do_cancels` still runs one-at-a-time |
| Session reuse | SDK `API.__init__` creates one persistent `requests.Session` per `Info`/`Exchange`; no per-request TLS |
| Constructor network I/O | `Exchange.__init__` builds its own `Info`, which fetches `spot_meta()`; production also builds a separate `Info` → duplicate `spot_meta` at startup only |
| `meta()` in hot path | No — `client.py` calls `meta()` once in `_init_clients` |
| Timeouts | Production `Info(...)`/`Exchange(...)` have **no timeout** (`timeout=None`); a stalled call blocks the thread and holds `_lock` forever |
| Signing | Local EIP-712 `sign_l1_action` p50 **4.46 ms**, p95 **5.46 ms** — negligible vs ACK |
| Actions used | `place_alo` → `exchange.order(..., cloid)`; `cancel` → `cancel_by_cloid`; `cancel_oid` → `cancel`; `cancel_all` → `bulk_cancel`. **No `modify`/`batchModify`** |
| Requote flow | `cycle()` → `_do_cancels(plan.cancels)` → `_do_places(plan.places)` sequentially, both legs serialized (≈2×cancel + 2×place) |
| `min_requote_interval_s` | 0.3 s; with ~2.2 s of serialized actions the bot cannot requote faster than the exchange responds |

---

## 2. Benchmark: REST vs WS vs modify (20 cycles each)

HYPE mid ~77.0. Values are p50 ms (p95 in parentheses). `sign` = local EIP-712 only.

| Path | n | sign | place ACK | modify ACK | cancel ACK | place+cancel |
|---|---:|---:|---:|---:|---:|---:|
| REST place / cancel by oid | 20 | 5.0 | 365.5 (483.1) | — | 668.4 (819.4) | **1073.3** (1192.8) |
| WS post place / cancel by oid | 20 | 5.0 | 392.0 (471.2) | — | 727.4 (821.6) | **1121.9** (1218.7) |
| REST place / batchModify / cancel | 20 | 5.0 | 357.8 (421.6) | **387.9 (420.5)** | 669.3 (777.4) | 1060.3 (1138.8) |
| WS post place / batchModify / cancel | 20 | 5.0 | 416.0 (576.4) | **399.7 (543.4)** | 710.9 (810.5) | 1130.6 (1261.1) |
| WS place / cancel by **cloid** | 19 | 4.8 | 415.8 (648.8) | — | 734.6 (886.1) | 1139.9 (1482.1) |
| REST place / cancel by **cloid** | 20 | 5.0 | 387.6 (472.4) | — | 724.1 (791.8) | 1098.0 (1240.0) |
| WS cancel+place **pipelined** (send both, don't wait) | 20 | 5.0 | 411.8 (478.9) | — | — | **713.3** to last ACK (797.6) |
| REST `Gtc` place / cancel | 20 | 4.9 | 677.6 (751.8) | — | 686.7 (760.4) | 1377.8 (1444.0) |
| WS `Gtc` place / cancel | 20 | 4.9 | 753.4 (983.4) | — | 702.1 (810.6) | 1453.1 (1750.8) |

Broadcast / delay-neutral:
- WS vs REST place: **no consistent win** (365 vs 392 p50; earlier runs saw WS
  faster and slower). Transport is not the bottleneck.
- `cloid` vs `oid` cancel: **no meaningful difference** (~724 vs ~728 p50).
- Place → wait → cancel (0/50/100/250/500 ms):
  cancel p50 = 684 / 689 / 723 / 700 / 744 ms → **order age/queue position does
  not matter**.

Additional focused tests (not in the 20-cycle table):

| Action | n | p50 | p95 |
|---|---:|---:|---:|
| Warm `/info` l2Book round trip (persistent session) | 15 | **6.0 ms** | 11.9 ms |
| Warm `/info` meta (17 KB payload) | 15 | 27.0 ms | 62.2 ms |
| Cancel **non-existent** oid (WS / REST) | 10 / 10 | **721.7 / 732.0 ms** | 840.0 / 800.8 ms |
| Two-leg `batchModify` (bid+ask in one action) | 20 | **409.9 ms** | 521.5 ms |
| `bulk_order` 2 legs (one action) | 10 | **482.8 ms** | 671.0 ms |
| `bulk_cancel` 2 legs (one action) | 10 | **803.3 ms** | 1029.1 ms |

---

## 3. Order replacement strategy

Current production: cancel each leg (~670-730 ms) then place each leg
(~360-420 ms), serialized → ~2.1-2.2 s for a two-leg requote.

Best measured replacements:

| Strategy | Actions | p50 | vs current |
|---|---:|---:|---:|
| Two-leg `batchModify` (both quotes, one action) | 1 | **~410 ms** | **~5.2× faster** |
| `bulk_order` (both new quotes, one action) | 1 | ~483 ms | ~4.5× faster |
| Pipelined cancel+place (don't wait for cancel ack) | 2 sent together | ~713 ms | ~3× faster |
| `bulk_cancel` (both legs, one action) | 1 | ~803 ms | ~2.7× faster for cancels only |

Critical detail: `batchModify` **returns a new oid** and the old oid becomes
invalid immediately:

```
request:  {"type":"batchModify","modifies":[{"oid":596779508403,"order":{...}}]}
response: {"status":"ok","response":{"type":"order","data":{"statuses":[
            {"resting":{"oid":596779512830}}]}}}
cancel old oid 596779508403 -> "Order was never placed, already canceled, or filled."
```

The production order manager must adopt the returned oid.

---

## 4. Network / edge routing report

- API host is fronted by **AWS CloudFront** (not Cloudflare; there is no
  `cf-ray`). Edge PoP observed: **`X-Amz-Cf-Pop: NRT20-P9`** (earlier
  `NRT57-P7`) — both Tokyo/Narita.
- Origin response: `Server: nginx`, `Via: …cloudfront.net`,
  `X-Cache: Miss from cloudfront`.
- DNS: `3.173.238.x` (CloudFront Tokyo).
- `ping` 10×: min/avg/max = 2.010 / **2.031** / 2.125 ms, 0% loss.
- `mtr -r -c 20`: hop 1 0.2 ms, hop 3 `be2999.ccr81.tyo01.atlas…` 1.9 ms,
  final hop `server-3-173-238-114.nrt2…` **1.6 ms**.
- `curl -w` cold connection (10 runs): dns 2.6-3.6 ms, connect 4.3-5.6 ms,
  TLS 45.9-50.9 ms, TTFB 63.0-72.3 ms, total 63.1-72.6 ms.
- Warm persistent session: l2Book **6.0 ms** p50.

Kernel / socket:

| Setting | Value | Note |
|---|---|---|
| `TCP_NODELAY` | on | set by urllib3 `default_socket_options` |
| `net.ipv4.tcp_fastopen` | 1 | |
| `net.core.busy_poll` / `busy_read` | 0 / 0 | Python does not set `SO_BUSY_POLL`; enabling the sysctl alone will not help |
| `net.ipv4.tcp_congestion_control` | cubic | only `reno cubic` available; **BBR unavailable** |
| `net.ipv4.tcp_slow_start_after_idle` | 1 | could be set to 0 for a long-lived idle action socket |
| `net.ipv4.tcp_notsent_lowat` | 4294967295 | |

Conclusion: the host is already on the nearest CloudFront edge (~2 ms RTT).
There is no meaningful network optimization left. TLS is ~46 ms cold but is
amortized to zero by the persistent session.

---

## 5. L1/consensus vs network breakdown

| Component | p50 | Share of a place ACK |
|---|---:|---:|
| Host → CloudFront edge RTT | ~2 ms | <1% |
| Warm round trip through edge→origin→edge (l2Book) | ~6 ms | ~1.5% |
| Cold TLS handshake (first request only) | ~46 ms | n/a after warmup |
| Alo place ACK | ~365-392 ms | 100% |
| Cancel ACK | ~668-735 ms | — |
| Gtc place ACK | ~678-753 ms | — |
| batchModify ACK | ~388-410 ms | — |

So **~360 ms of a place ACK and ~660 ms of a cancel ACK are server-side /
L1 / matching-engine time**, not Tokyo→edge transit.

### Why is cancel ~270 ms slower than place?

Measured facts:
1. Cancelling a **non-existent** oid still takes ~722-732 ms — the slow part is
   the cancel action envelope, not book mutation.
2. Delay sweep (0-500 ms between place and cancel) changes nothing.
3. In the pipelined test the cancel is sent **first**, yet its ACK arrives
   ~650-740 ms while the place sent immediately after arrives at ~380-450 ms.
4. `Gtc` placement is also ~700 ms, while `Alo` placement is ~400 ms.

Interpretation: Hyperliquid processes these actions on two different
server-side paths/latencies — a fast path for non-crossing `Alo` orders and
`batchModify` (~400 ms), and a slower path for `cancel` and `Gtc` (~700 ms).
This is an exchange/L1 scheduling property; it is not network, not queue
position, not order age, and not `cloid` vs `oid`.

Practical consequence: **`Alo` is the right TIF (already used), and requotes
should use `batchModify`, never cancel+place.** True cancels remain ~700 ms and
should be batched and kept off the critical requote path.

---

## 6. Required `src/mmbot/` changes (in priority order)

1. **Add `modify`/`batchModify` to `HyperliquidGateway`** — new
   `modify_quotes(modifies)` calling `exchange.bulk_modify_orders_new` with all
   legs in one action. Parse `statuses[*].resting.oid`, return the new oid, and
   update the order manager (`OrderManager`) immediately.
2. **Rewrite `main.cycle()` requote diff** — replace
   `_do_cancels(plan.cancels)` + `_do_places(plan.places)` with:
   - one `batchModify` for every resting leg whose price/size changed,
   - one `bulk_order` for legs with no live order,
   - one `bulk_cancel` only for legs that must be removed (risk-off/stale).
   This reduces a two-leg reprice from ~2.1 s to ~0.41 s p50.
3. **Use bulk placement/cancel** — `exchange.bulk_orders` / `bulk_cancel`
   instead of N serialized single calls (2-leg place 483 ms, 2-leg cancel
   803 ms).
4. **Order manager must track the latest oid** — `batchModify` invalidates the
   old oid and returns a new one; `cloid` is not preserved on the modified
   order. Reconciliation must read `open_orders` after any ambiguous response.
5. **Add explicit timeouts** — `Info(..., timeout=10)` and
   `Exchange(..., timeout=10)`. Today they are `None`; one stalled request
   holds `_lock` indefinitely.
6. **Monotonic nonce** — `max(get_timestamp_ms(), last_nonce + 1)`. A retry of
   the same signed payload returns `Invalid nonce: duplicate nonce`; treat that
   as “first attempt may have landed” and reconcile, never blind-retry as a
   failure.
7. **Transport** — keep the persistent `requests.Session` (already there). WS
   `post` is optional: it showed no consistent ACK win and needs ping
   keepalive (server closed an idle socket after ~40 s) plus reconnect. Do not
   prioritize it over modify/bulk; it is not where the latency is.
8. **Avoid duplicate constructor I/O** — pass cached `meta`/`spot_meta` into
   `Info`/`Exchange` so `Exchange` does not refetch `spot_meta` at startup.
9. **Free the event loop** — with one 400 ms modify instead of four serialized
   calls (~2.2 s), `cycle()` holds `_lock` far less; keep `min_requote_interval`
   and the feed watchdog unchanged initially.
10. **Latency budget** — post-change expected p50: modify ~410 ms, place
    ~390 ms, cancel ~700 ms. This is still far above the PREREG2 replay cells
    (66/150 ms). PREREG2 was **not** touched.

---

## 7. Bottom line

- Local signing is irrelevant (4.5 ms).
- Network/edge is already optimal (~2 ms RTT to NRT; ~6 ms warm round trip).
- Transport (REST vs WS) is within noise; L1 action processing dominates.
- The dominant fix is architectural: **stop doing cancel+place; update quotes
  with one two-leg `batchModify`** (~410 ms vs ~2,100 ms), and batch
  placements/cancels when they are unavoidable.
- Cancels are inherently ~700 ms and ~270 ms slower than `Alo` places; this is
  an exchange-side action-class difference, not a network or client issue.
