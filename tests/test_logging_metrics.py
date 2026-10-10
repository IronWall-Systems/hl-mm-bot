"""T3: file logging, secret redaction, metrics line/loop. All offline (no network)."""
import asyncio
import logging
from logging.handlers import RotatingFileHandler

from mmbot.config import Settings, load_settings
from mmbot.exchange.fake import FakeGateway
from mmbot.main import Bot, setup_file_logging
from mmbot.models import RiskConfig, StrategyParams

SECRET = "0x0123456789abcdef0123456789abcdef01234567"   # fake, test-only


class Clock:
    def __init__(self): self.t = 100.0
    def __call__(self): return self.t


def make_settings() -> Settings:
    return Settings(env="testnet", account_address="0x1234567890abcdef", api_secret=SECRET,
                    coin="BTC", dry_run=True, flatten_on_exit=True,
                    strategy=StrategyParams(), risk=RiskConfig())


def test_settings_repr_hides_api_secret():
    s = make_settings()
    assert SECRET not in repr(s)
    assert SECRET not in str(s)
    assert SECRET not in "%s" % s                     # logging %-format path
    assert "api_secret" not in repr(s)                # field omitted entirely, not just masked
    s2 = load_settings({"HL_ENV": "testnet", "HL_ACCOUNT_ADDRESS": "0xabc",
                        "HL_API_SECRET": SECRET, "HL_DRY_RUN": "1"})
    assert SECRET not in repr(s2)                     # loader output is safe to log too


def test_setup_file_logging_rotates(tmp_path):
    root = logging.getLogger()
    before = list(root.handlers)
    saved_lvl = root.level
    try:
        setup_file_logging(str(tmp_path))
        added = [h for h in root.handlers if h not in before]
        assert len(added) == 1
        h = added[0]
        assert isinstance(h, RotatingFileHandler)
        assert h.maxBytes == 5 * 1024 * 1024 and h.backupCount == 5
        root.setLevel(logging.INFO)
        logging.getLogger("mmbot").info("rotating-log-marker")
        logging.getLogger("mmbot").info("startup settings=%s", make_settings())  # realistic log call
        h.flush()
        text = (tmp_path / "bot.log").read_text()
        assert "rotating-log-marker" in text
        assert SECRET not in text                        # file log can never carry the key
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
                h.close()
        root.setLevel(saved_lvl)


def test_metrics_line_contents_and_rss_budget():
    async def go():
        gw = FakeGateway()
        clk = Clock()
        bot = Bot(gw, StrategyParams(quote_notional=20, max_notional=60, min_requote_interval_s=0.0),
                  RiskConfig(max_order_notional=25, max_position_notional=80), now_fn=clk)
        await bot.init()
        gw.set_book(69999, 70001)
        while not gw.events.empty():
            await bot.handle_event(gw.events.get_nowait())
        await bot.cycle()
        line = bot.metrics_line()
        for token in ("metrics ", "book=1", "fills=0", "places=2", "cancels=", "cross_rejects=",
                      "risk_rejects=", "other_rejects=", "place_errors=", "stale_pulls=",
                      "feed_restarts=", "pos_drift=", "orphans_cancelled=", "stale_pruned=",
                      "actions_min=", "lat_ema_ms=", "pos=", "equity=1000.00", "rss_peak_mb=",
                      "feed_age="):                            # T7e: feed age observable
            assert token in line, f"missing {token!r} in {line!r}"
        rss = float(line.split("rss_peak_mb=")[1].split()[0])
        assert rss < 300, f"peak RSS {rss:.0f} MB exceeds the 300 MB budget"
        assert SECRET not in line                      # metrics never carry secrets
    asyncio.run(go())


def test_metrics_loop_emits_and_stops():
    async def go():
        gw = FakeGateway()
        bot = Bot(gw, StrategyParams(), RiskConfig(), now_fn=Clock())
        await bot.init()
        bot.metrics_interval_s = 0.05
        records = []

        class Cap(logging.Handler):
            def emit(self, r):
                records.append(r.getMessage())

        cap = Cap()
        mlog = logging.getLogger("mmbot")
        saved_lvl = mlog.level
        mlog.addHandler(cap)
        mlog.setLevel(logging.INFO)
        try:
            task = asyncio.create_task(bot.metrics_loop())
            await asyncio.sleep(0.17)                 # immediate line + >=1 interval line
            bot.stop_event.set()
            await asyncio.wait_for(task, timeout=1.0)
        finally:
            mlog.removeHandler(cap)
            mlog.setLevel(saved_lvl)
        hits = [r for r in records if r.startswith("metrics ")]
        assert len(hits) >= 2, f"expected >=2 metrics lines, got {hits!r}"
        assert all("rss_peak_mb=" in h for h in hits)
    asyncio.run(go())
