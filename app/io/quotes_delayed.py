"""
Delayed quotes ingestor using Yahoo Finance public chart API (best-effort).
Produces simple OHLCV bars for a small watchlist, suitable for demo/Day3.

Env:
- TICKERS: comma-separated symbols
- BAR_SEC: 30 or 60 (default 30). Yahoo offers 1m; 30s bars are approximated.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

import httpx


@dataclass
class Candle:
    ticker: str
    ts: datetime
    o: float
    h: float
    l: float
    c: float
    v: int
    spread_est: float = 0.0


class DelayedQuotesIngestor:
    def __init__(self, tickers_csv: Optional[str] = None, bar_sec: int = 30):
        self.tickers: List[str] = [t.strip().upper() for t in (tickers_csv or os.getenv("TICKERS", "AAPL,MSFT")).split(",") if t.strip()]
        self.bar_sec: int = int(os.getenv("BAR_SEC", str(bar_sec)))
        if self.bar_sec not in (15, 30, 60):
            self.bar_sec = 30
        self.market_data: Dict[str, Dict] = {}

    def _fetch_yahoo_1m(self, ticker: str) -> Dict:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=1d&interval=1m"
        headers = {"User-Agent": os.getenv("SEC_USER_AGENT", "curl/7")}
        with httpx.Client(headers=headers, timeout=10.0) as client:
            r = client.get(url)
            r.raise_for_status()
            return r.json()

    def _parse_1m_to_candles(self, ticker: str, data: Dict) -> List[Candle]:
        try:
            result = data["chart"]["result"][0]
            ts_arr = result.get("timestamp", [])
            ind = result.get("indicators", {}).get("quote", [{}])[0]
            opens = ind.get("open", [])
            highs = ind.get("high", [])
            lows = ind.get("low", [])
            closes = ind.get("close", [])
            vols = ind.get("volume", [])
        except Exception:
            return []
        candles: List[Candle] = []
        for i, ts in enumerate(ts_arr or []):
            try:
                cndl = Candle(
                    ticker=ticker,
                    ts=datetime.fromtimestamp(int(ts), tz=timezone.utc),
                    o=float(opens[i] or closes[i]),
                    h=float(highs[i] or closes[i]),
                    l=float(lows[i] or closes[i]),
                    c=float(closes[i] or 0.0),
                    v=int(vols[i] or 0),
                    spread_est=0.0,
                )
                candles.append(cndl)
            except Exception:
                continue
        # If BAR_SEC==30 or 15, approximate by splitting each 1m bar
        if self.bar_sec in (30, 15) and candles:
            split: List[Candle] = []
            factor = 2 if self.bar_sec == 30 else 4
            for c in candles:
                vol_per = max(c.v // factor, 0)
                for _ in range(factor):
                    split.append(Candle(
                        ticker=c.ticker,
                        ts=c.ts,  # keep same ts; downstream uses ordering not exact spacing
                        o=c.o, h=c.h, l=c.l, c=c.c, v=vol_per, spread_est=c.spread_est
                    ))
            candles = split
        return candles[-200:]

    def update_all_tickers(self) -> None:
        for t in self.tickers:
            try:
                raw = self._fetch_yahoo_1m(t)
                candles = self._parse_1m_to_candles(t, raw)
                last_price = candles[-1].c if candles else 0.0
                self.market_data[t] = {
                    "candles": candles,
                    "current_price": last_price,
                    "indicators": self._compute_indicators_from_candles(candles),
                    "last_update": datetime.now(timezone.utc),
                }
            except Exception:
                continue

    def get_latest_candles(self, ticker: str, n: int = 50) -> List[Candle]:
        c = self.market_data.get(ticker, {}).get("candles", [])
        return c[-n:]

    def get_technical_indicators(self, ticker: str) -> Dict:
        # simple placeholder; real indicators computed elsewhere
        md = self.market_data.get(ticker, {})
        return {
            "current_price": md.get("current_price", 0.0),
            "dollar_vol_5m": md.get("indicators", {}).get("dollar_vol_5m", 0.0),
            "spread_bp": md.get("indicators", {}).get("spread_bp", 0.0)
        }

    def get_market_data_summary(self) -> Dict[str, Dict]:
        return {t: {"current_price": md.get("current_price"), "indicators": md.get("indicators"), "last_update": md.get("last_update")} for t, md in self.market_data.items()}

    def _compute_indicators_from_candles(self, candles: List[Candle]) -> Dict:
        if not candles:
            return {"dollar_vol_5m": 0.0, "spread_bp": 0.0}
        # last 5 minutes window approx: use last N bars corresponding to 5 minutes
        bars_per_min = 60 // max(self.bar_sec, 1)
        n = min(len(candles), bars_per_min * 5)
        window = candles[-n:]
        last_close = window[-1].c if window else 0.0
        dollar_vol = sum((c.c or last_close) * float(c.v or 0) for c in window)
        # rough spread estimate from last bar high/low
        last = window[-1]
        if last.c > 0:
            spread_bp = ((last.h - last.l) / max(last.c, 1e-9)) * 10000.0
        else:
            spread_bp = 0.0
        return {"dollar_vol_5m": float(dollar_vol), "spread_bp": float(spread_bp)}

    def update_universe_tickers(self, new_tickers: List[str]) -> None:
        new_set = [t.strip().upper() for t in new_tickers if t and t.strip()]
        # start/stop: simply replace; warmup will backfill for new ones
        added = [t for t in new_set if t not in self.tickers]
        removed = [t for t in self.tickers if t not in new_set]
        self.tickers = new_set
        # cleanup removed
        for t in removed:
            if t in self.market_data:
                try:
                    del self.market_data[t]
                except Exception:
                    pass
        # warmup fetch for added symbols
        if added:
            try:
                self.warmup_backfill(added)
            except Exception:
                pass

    def warmup_backfill(self, symbols: Optional[List[str]] = None) -> None:
        """Fetch recent data quickly to avoid empty buffers on start/universe change."""
        target = symbols or list(self.tickers)
        for t in target:
            try:
                raw = self._fetch_yahoo_1m(t)
                candles = self._parse_1m_to_candles(t, raw)
                last_price = candles[-1].c if candles else 0.0
                self.market_data[t] = {
                    "candles": candles,
                    "current_price": last_price,
                    "indicators": self._compute_indicators_from_candles(candles),
                    "last_update": datetime.now(timezone.utc),
                }
            except Exception:
                continue


