#!/usr/bin/env python3
"""scripts/breakout_live.py — Blackout-Proof 1H Breakout Live Daemon with On-Chain SL Triggers."""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

from breakout_backtest import compute_indicators, fetch_1h_candles

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "logs"
STATE_FILE = DATA_DIR / "breakout_state.json"
HL_INFO_URL = "https://api.hyperliquid.xyz/info"

# 24h circuit breaker: a realized trade or UTC-day loss worse than this pauses entries.
LOSS_COOLDOWN_USD = float(os.getenv("HL_LOSS_COOLDOWN_USD", "0.25") or 0.25)
COOLDOWN_HOURS = float(os.getenv("HL_COOLDOWN_HOURS", "24") or 24)


def setup_logger() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("breakout_live")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh = logging.FileHandler(LOG_DIR / "breakout_live.log", encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


def round_px(px: float, sz_decimals: int = 2) -> float:
    """Hyperliquid Perp rule: max 5 significant figures AND max (6 - szDecimals) decimals."""
    if px <= 0:
        return 0.0
    max_dec = max(0, 6 - sz_decimals)
    sig_dec = 5 - int(math.floor(math.log10(abs(px)))) - 1
    dec = min(max_dec, max(0, sig_dec))
    return float(f"{round(px, dec):.{dec}f}")


def round_sz(sz: float, sz_decimals: int = 2) -> float:
    return float(f"{round(sz, sz_decimals):.{sz_decimals}f}")


def hl_info(payload: dict) -> dict | list:
    resp = requests.post(HL_INFO_URL, json=payload, timeout=10)
    resp.raise_for_status()
    return resp.json()


def get_sz_decimals(coin: str = "HYPE") -> int:
    meta = hl_info({"type": "meta"})
    for asset in meta.get("universe", []):
        if asset.get("name") == coin:
            return int(asset.get("szDecimals", 2))
    return 2


def get_account_state(user_addr: str, coin: str = "HYPE") -> dict:
    ch = hl_info({"type": "clearinghouseState", "user": user_addr})
    equity = float(ch.get("marginSummary", {}).get("accountValue", 0.0))
    pos_szi = 0.0
    entry_px = 0.0
    for p in ch.get("assetPositions", []):
        pos = p.get("position", {})
        if pos.get("coin") == coin:
            pos_szi = float(pos.get("szi", 0.0))
            entry_px = float(pos.get("entryPx", 0.0) or 0.0)
            break

    open_orders = hl_info({"type": "frontendOpenOrders", "user": user_addr})
    coin_orders = [o for o in open_orders if o.get("coin") == coin]
    sl_orders = [
        o for o in coin_orders
        if o.get("reduceOnly") is True and (o.get("isTrigger") is True or "Stop" in str(o.get("orderType", "")))
    ]
    return {"equity": equity, "pos_szi": pos_szi, "entry_px": entry_px, "all_orders": coin_orders, "sl_orders": sl_orders}


def get_top_of_book(coin: str = "HYPE") -> tuple[float, float]:
    book = hl_info({"type": "l2Book", "coin": coin})
    levels = book.get("levels", [[], []])
    best_bid = float(levels[0][0]["px"])
    best_ask = float(levels[1][0]["px"])
    return best_bid, best_ask


def load_state(equity_default: float = 30.0) -> dict:
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    if STATE_FILE.exists():
        try:
            st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if st.get("day_utc") != today:
                st["day_utc"] = today
                st["daily_stopouts"] = 0
                st["day_start_equity"] = equity_default
                st["day_realized_pnl"] = 0.0
            st.setdefault("cooldown_until_ms", 0)
            st.setdefault("day_realized_pnl", 0.0)
            st.setdefault("last_equity", equity_default)
            return st
        except Exception:
            pass
    return {
        "day_utc": today, "daily_stopouts": 0, "day_start_equity": equity_default,
        "day_realized_pnl": 0.0, "cooldown_until_ms": 0, "last_equity": equity_default,
        "active_trade": None, "last_bar_ms": 0,
    }


def arm_cooldown(st: dict, now_ms: int, reason: str, net_pnl: float, logger: logging.Logger) -> None:
    until = now_ms + int(COOLDOWN_HOURS * 3600 * 1000)
    if until > st.get("cooldown_until_ms", 0):
        st["cooldown_until_ms"] = until
    logger.warning(
        f"[COOLDOWN] {reason}: loss ${net_pnl:+.2f} breached -${LOSS_COOLDOWN_USD:.2f}; "
        f"no new entries until {datetime.fromtimestamp(until / 1000.0, tz=timezone.utc).isoformat()}"
    )


def save_state(st: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def load_credentials() -> tuple[str, str]:
    load_dotenv(PROJECT_ROOT / ".env")
    pk = (
        os.getenv("HL_API_SECRET")
        or os.getenv("HL_PRIVATE_KEY")
        or os.getenv("PRIVATE_KEY")
        or os.getenv("ETH_PRIVATE_KEY")
        or os.getenv("SECRET_KEY")
        or ""
    ).strip()
    if not pk:
        raise RuntimeError("Missing private key in .env")
    if not pk.startswith("0x"):
        pk = "0x" + pk
    acct = Account.from_key(pk)
    user_addr = (
        os.getenv("HL_ACCOUNT_ADDRESS")
        or os.getenv("HL_WALLET_ADDRESS")
        or os.getenv("WALLET_ADDRESS")
        or os.getenv("ACCOUNT_ADDRESS")
        or acct.address
    ).strip()
    return pk, user_addr


def place_onchain_sl(exchange: Exchange, coin: str, is_long: bool, sz: float, sl_px: float, sz_dec: int, logger: logging.Logger) -> dict:
    """Submit native Hyperliquid on-chain trigger stop-loss (`reduce_only=True`)."""
    sl_px_clean = round_px(sl_px, sz_dec)
    is_buy = not is_long
    limit_px = round_px(sl_px_clean * 0.97 if is_long else sl_px_clean * 1.03, sz_dec)
    sz_clean = round_sz(abs(sz), sz_dec)
    order_type = {"trigger": {"triggerPx": sl_px_clean, "isMarket": True, "tpsl": "sl"}}
    res = exchange.order(coin, is_buy, sz_clean, limit_px, order_type, True)
    logger.info(f"Placed On-Chain SL: side={'SELL' if is_long else 'BUY'} sz={sz_clean} triggerPx={sl_px_clean} limitPx={limit_px} -> {res}")
    return res


def reconcile_and_heal(exchange: Exchange | None, user_addr: str, coin: str, sz_dec: int, st: dict, mode: str, logger: logging.Logger) -> dict:
    """Self-heal on startup & every 60s: guarantee open positions ALWAYS have an on-chain stop-loss."""
    acct = get_account_state(user_addr, coin)
    pos_szi = acct["pos_szi"]
    sl_orders = acct["sl_orders"]

    # Case A: Exchange has an open position, but NO active on-chain SL trigger!
    if abs(pos_szi) > 0 and len(sl_orders) == 0:
        candles = fetch_1h_candles(coin, lookback_days=5)
        ind = compute_indicators(candles, len(candles) - 1)
        atr = ind["atr_14"] if ind else acct["entry_px"] * 0.02
        is_long = pos_szi > 0
        emerg_sl = round_px(acct["entry_px"] - 1.5 * atr if is_long else acct["entry_px"] + 1.5 * atr, sz_dec)
        logger.warning(f"[SELF-HEAL] Open position ({pos_szi}) found WITHOUT on-chain SL! Placing emergency SL @ {emerg_sl}")
        if mode == "live" and exchange is not None:
            place_onchain_sl(exchange, coin, is_long, abs(pos_szi), emerg_sl, sz_dec, logger)
        st["active_trade"] = {
            "side": "LONG" if is_long else "SHORT",
            "entry_px": acct["entry_px"],
            "sz": abs(pos_szi),
            "sl_px": emerg_sl,
            "extreme_px": acct["entry_px"],
        }
        save_state(st)

    # Case B: State thinks trade is open, but exchange position is 0 (SL triggered while offline)
    elif abs(pos_szi) == 0 and st.get("active_trade") is not None:
        logger.info("[SELF-HEAL] Exchange position is now 0. Clearing active_trade and orphaned orders.")
        if mode == "live" and exchange is not None:
            for o in acct["all_orders"]:
                exchange.cancel(coin, int(o["oid"]))
        realized = acct["equity"] - st.get("last_equity", acct["equity"])
        st["day_realized_pnl"] = st.get("day_realized_pnl", 0.0) + realized
        logger.info(f"[CLOSE] realized≈${realized:+.2f} | day realized≈${st['day_realized_pnl']:+.2f}")
        if realized < -LOSS_COOLDOWN_USD or st["day_realized_pnl"] < -LOSS_COOLDOWN_USD:
            arm_cooldown(st, int(time.time() * 1000), "trade/day loss", realized, logger)
        if acct["equity"] < st.get("day_start_equity", acct["equity"]):
            st["daily_stopouts"] = st.get("daily_stopouts", 0) + 1
        st["active_trade"] = None
        st["last_equity"] = acct["equity"]
        save_state(st)

    return acct


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--coin", default="HYPE")
    parser.add_argument("--mode", choices=["dry_run", "live"], default="dry_run")
    parser.add_argument("--once", action="store_true", help="Run a single check cycle and exit")
    args = parser.parse_args()

    logger = setup_logger()
    pk, user_addr = load_credentials()
    sz_dec = get_sz_decimals(args.coin)
    logger.info(f"Starting breakout_live | mode={args.mode} | coin={args.coin} | szDecimals={sz_dec} | user={user_addr}")

    exchange = None
    if args.mode == "live":
        wallet = Account.from_key(pk)
        exchange = Exchange(wallet, constants.MAINNET_API_URL, account_address=user_addr)
        try:
            exchange.update_leverage(3, args.coin, is_cross=True)
        except Exception as e:
            logger.warning(f"Leverage update notice: {e}")

    acct0 = get_account_state(user_addr, args.coin)
    st = load_state(equity_default=acct0["equity"] or 30.0)
    save_state(st)

    while True:
        try:
            acct = reconcile_and_heal(exchange, user_addr, args.coin, sz_dec, st, args.mode, logger)
            candles = fetch_1h_candles(args.coin, lookback_days=7)
            last_bar = candles[-1]
            ind = compute_indicators(candles, len(candles) - 1)

            if ind and last_bar["ts_ms"] > st.get("last_bar_ms", 0):
                st["last_bar_ms"] = last_bar["ts_ms"]
                save_state(st)
                logger.info(
                    f"1H Close={last_bar['close']} | HH_20={ind['hh_20']:.4f} | LL_20={ind['ll_20']:.4f} "
                    f"| ATR_14={ind['atr_14']:.4f} | VolRatio={ind['vol_ratio']:.2f} | Eq=${acct['equity']:.2f}"
                )

                # If in position, update Chandelier trailing stop (place NEW SL before canceling OLD SL)
                if abs(acct["pos_szi"]) > 0 and st.get("active_trade"):
                    tr = st["active_trade"]
                    is_long = acct["pos_szi"] > 0
                    best_bid, best_ask = get_top_of_book(args.coin)
                    mid = (best_bid + best_ask) / 2.0
                    if is_long:
                        tr["extreme_px"] = max(tr.get("extreme_px", last_bar["high"]), last_bar["high"])
                        cand_sl = round_px(tr["extreme_px"] - 3.0 * ind["atr_14"], sz_dec)
                        should_move = cand_sl > tr["sl_px"] and cand_sl < mid
                    else:
                        tr["extreme_px"] = min(tr.get("extreme_px", last_bar["low"]), last_bar["low"])
                        cand_sl = round_px(tr["extreme_px"] + 3.0 * ind["atr_14"], sz_dec)
                        should_move = cand_sl < tr["sl_px"] and cand_sl > mid

                    if should_move:
                        logger.info(f"Trailing SL update: {tr['sl_px']} -> {cand_sl}")
                        if args.mode == "live" and exchange is not None:
                            old_sls = acct["sl_orders"]
                            res = place_onchain_sl(exchange, args.coin, is_long, abs(acct["pos_szi"]), cand_sl, sz_dec, logger)
                            if res.get("status") == "ok":
                                for old_o in old_sls:
                                    exchange.cancel(args.coin, int(old_o["oid"]))
                        tr["sl_px"] = cand_sl
                        save_state(st)

                # If flat, check entry conditions
                elif abs(acct["pos_szi"]) == 0:
                    day_pnl = acct["equity"] - st.get("day_start_equity", acct["equity"])
                    now_ms = int(time.time() * 1000)
                    cooldown_until = st.get("cooldown_until_ms", 0)
                    if now_ms < cooldown_until:
                        would_sig = ("LONG" if last_bar["close"] > ind["hh_20"]
                                     else ("SHORT" if last_bar["close"] < ind["ll_20"] else None))
                        if would_sig:
                            logger.info(
                                f"[COOLDOWN] active until "
                                f"{datetime.fromtimestamp(cooldown_until / 1000.0, tz=timezone.utc).isoformat()}"
                                f" — blocked {would_sig} signal"
                            )
                    elif acct["equity"] >= 15.0 and st.get("daily_stopouts", 0) < 2 and day_pnl > -1.20 and ind["vol_ratio"] >= 1.20:
                        sig = "LONG" if last_bar["close"] > ind["hh_20"] else ("SHORT" if last_bar["close"] < ind["ll_20"] else None)
                        if sig:
                            is_long = sig == "LONG"
                            best_bid, best_ask = get_top_of_book(args.coin)
                            px = round_px(best_bid if is_long else best_ask, sz_dec)
                            notional = max(12.0, min(round(0.50 * acct["equity"], 2), acct["equity"] * 0.90))
                            sz = round_sz(notional / px, sz_dec)
                            if sz * px < 10.5:
                                sz = round_sz(11.0 / px, sz_dec)
                            sl_px = round_px(px - 1.5 * ind["atr_14"] if is_long else px + 1.5 * ind["atr_14"], sz_dec)
                            logger.info(f"ENTRY SIGNAL: {sig} sz={sz} @ {px} | Initial SL={sl_px}")

                            if args.mode == "live" and exchange is not None:
                                res = exchange.order(args.coin, is_long, sz, px, {"limit": {"tif": "Alo"}}, False)
                                logger.info(f"Alo Entry Order response: {res}")
                                time.sleep(10)
                                acct_after = get_account_state(user_addr, args.coin)
                                for o in acct_after["all_orders"]:
                                    if not o.get("reduceOnly"):
                                        exchange.cancel(args.coin, int(o["oid"]))
                                if abs(acct_after["pos_szi"]) > 0:
                                    place_onchain_sl(exchange, args.coin, is_long, abs(acct_after["pos_szi"]), sl_px, sz_dec, logger)
                                    st["active_trade"] = {
                                        "side": sig,
                                        "entry_px": acct_after["entry_px"] or px,
                                        "sz": abs(acct_after["pos_szi"]),
                                        "sl_px": sl_px,
                                        "extreme_px": last_bar["high"] if is_long else last_bar["low"],
                                    }
                                    save_state(st)

            st["last_equity"] = acct["equity"]
            save_state(st)

            if args.once:
                logger.info("Single check cycle (--once) completed successfully.")
                break

        except Exception as e:
            logger.error(f"Loop error: {e}", exc_info=True)
            if args.once:
                raise

        time.sleep(60)


if __name__ == "__main__":
    main()
