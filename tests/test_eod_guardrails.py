import types
import time as _time

import pytest


class DummyPos:
    def __init__(self, ticker, qty):
        self.ticker = ticker
        self.quantity = qty


class DummyTrade:
    def __init__(self, trade_id="T1", ticker="TZA", side="sell", qty=1, price=10.0):
        self.trade_id = trade_id
        self.ticker = ticker
        self.side = side
        self.quantity = qty
        self.price = price


def test_flatten_all_positions_continues_on_failure(monkeypatch):
    import app.jobs.scheduler as sched

    # positions: A fails once then succeeds, B succeeds
    class DummyAdapter:
        def __init__(self):
            self.calls = {"A": 0, "B": 0}

        def get_positions(self):
            return [DummyPos("A", 10), DummyPos("B", 5)]

        def submit_eod_exit(self, symbol, qty, side="sell"):
            self.calls[symbol] += 1
            if symbol == "A" and self.calls[symbol] == 1:
                raise Exception("Market is closed")
            return DummyTrade(trade_id=f"ORD_{symbol}", ticker=symbol, qty=qty)

    # speed up retries
    monkeypatch.setattr(sched, "EOD_MAX_RETRIES", 1, raising=False)
    monkeypatch.setattr(sched, "EOD_RETRY_DELAY_SEC", 0, raising=False)

    # stub DB helpers
    monkeypatch.setattr(sched, "save_signal_to_db", lambda *args, **kwargs: "1", raising=False)
    monkeypatch.setattr(sched, "save_trade_to_db", lambda *args, **kwargs: "1", raising=False)

    adapter = DummyAdapter()
    flattened = sched.flatten_all_positions(adapter, reason="test_eod")
    assert flattened == 2


def test_time_stop_exits_on_age(monkeypatch):
    import app.jobs.scheduler as sched

    # enable time stop
    monkeypatch.setattr(sched, "ENABLE_TIME_STOP", True, raising=False)
    # force RTH
    monkeypatch.setattr(sched, "_is_rth_now", lambda *args, **kwargs: True, raising=False)

    # fake redis client
    class FakeRedis:
        def __init__(self):
            self.store = {}

        def get(self, k):
            return self.store.get(k)

        def setex(self, *args, **kwargs):
            return True

    fake_redis_mod = types.SimpleNamespace(
        Redis=types.SimpleNamespace(from_url=lambda *args, **kwargs: FakeRedis())
    )
    monkeypatch.setattr(sched, "redis", fake_redis_mod, raising=False)

    now = 1_000_000
    entry_time = now - (sched.TIME_STOP_MIN * 60 + 60)

    # adapter that provides one position, and records if exited
    class DummyAdapter:
        def __init__(self):
            self.exits = []

        def get_positions(self):
            return [DummyPos("TZA", 3)]

        def submit_eod_exit(self, symbol, qty, side="sell"):
            self.exits.append((symbol, qty))
            return DummyTrade(trade_id=f"TS_{symbol}", ticker=symbol, qty=qty)

        def submit_market_order(self, *args, **kwargs):
            self.exits.append((kwargs.get("ticker"), kwargs.get("quantity")))
            return DummyTrade(trade_id=f"MO_{kwargs.get('ticker')}", ticker=kwargs.get("ticker"), qty=kwargs.get("quantity"))

        def get_current_price(self, *args, **kwargs):
            return 10.0

    # provide adapter via real import path monkeypatch
    monkeypatch.setenv("REDIS_URL", "redis://local:6379/0")
    import app.adapters.trading_adapter as ta
    monkeypatch.setattr(ta, "get_trading_adapter", lambda: DummyAdapter(), raising=False)

    # patch redis to contain entry time
    # since enforce_time_stop creates a new FakeRedis(), patch its get via monkeypatching inside function
    def fake_from_url(*args, **kwargs):
        fr = FakeRedis()
        fr.store["position_entry_time:TZA"] = str(entry_time)
        return fr

    fake_redis_mod.Redis = types.SimpleNamespace(from_url=fake_from_url)
    monkeypatch.setattr(sched, "redis", fake_redis_mod, raising=False)

    # patch time.time
    monkeypatch.setattr(sched.time, "time", lambda: now, raising=False)

    # run
    res = sched.enforce_time_stop(None)
    assert isinstance(res, dict)
    assert res.get("exited", 0) == 1
