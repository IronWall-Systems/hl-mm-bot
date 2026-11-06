from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field
from typing import Mapping

from .models import RiskConfig, StrategyParams

URLS = {"testnet": "https://api.hyperliquid-testnet.xyz", "mainnet": "https://api.hyperliquid.xyz"}
MAINNET_CONFIRM = "YES_ISOLATED_WALLET"
MAINNET_SOFT_CAP_USD = 250.0


@dataclass
class Settings:
    env: str
    account_address: str
    api_secret: str = field(repr=False)     # never printed
    coin: str
    dry_run: bool
    flatten_on_exit: bool
    strategy: StrategyParams
    risk: RiskConfig

    @property
    def base_url(self) -> str:
        return URLS[self.env]


def _apply(obj, prefix: str, environ: Mapping[str, str]):
    kw = {}
    for f in dataclasses.fields(obj):
        key = prefix + f.name.upper()
        if key in environ:
            cur = getattr(obj, f.name)
            raw = environ[key]
            kw[f.name] = (raw.lower() in ("1", "true", "yes")) if isinstance(cur, bool) else type(cur)(raw)
    return dataclasses.replace(obj, **kw) if kw else obj


def load_settings(environ: Mapping[str, str] = os.environ) -> Settings:
    env = environ.get("HL_ENV", "testnet").lower()
    if env not in URLS:
        raise SystemExit(f"HL_ENV must be testnet or mainnet, got {env!r}")
    coin = environ.get("HL_COIN", "BTC")
    strat = _apply(StrategyParams(coin=coin), "MM_", environ)
    risk = _apply(RiskConfig(), "RISK_", environ)
    for need in ("HL_ACCOUNT_ADDRESS", "HL_API_SECRET"):
        if not environ.get(need):
            raise SystemExit(f"missing env var {need}")

    if env == "mainnet":
        if environ.get("HL_CONFIRM_MAINNET") != MAINNET_CONFIRM:
            raise SystemExit(f"mainnet requires HL_CONFIRM_MAINNET={MAINNET_CONFIRM}")
        if risk.max_position_notional > MAINNET_SOFT_CAP_USD and environ.get("HL_ALLOW_LARGE") != "1":
            raise SystemExit(f"max_position_notional > ${MAINNET_SOFT_CAP_USD:.0f}: set HL_ALLOW_LARGE=1 deliberately")

    if strat.quote_notional < strat.min_order_notional:
        raise SystemExit("quote_notional below min_order_notional")
    if strat.quote_notional > risk.max_order_notional:
        raise SystemExit("quote_notional exceeds RiskConfig.max_order_notional")
    if strat.max_notional > risk.max_position_notional:
        raise SystemExit("strategy max_notional exceeds RiskConfig.max_position_notional")
    if risk.max_account_loss_usd < 0:
        raise SystemExit("RISK_MAX_ACCOUNT_LOSS_USD must be >= 0 (0 disables)")
    if risk.emergency_flatten_notional < 0:
        raise SystemExit("RISK_EMERGENCY_FLATTEN_NOTIONAL must be >= 0 (0 disables)")

    return Settings(env=env, account_address=environ["HL_ACCOUNT_ADDRESS"], api_secret=environ["HL_API_SECRET"],
                    coin=coin, dry_run=environ.get("HL_DRY_RUN", "1") != "0",
                    flatten_on_exit=environ.get("HL_FLATTEN_ON_EXIT", "1") != "0",
                    strategy=strat, risk=risk)
