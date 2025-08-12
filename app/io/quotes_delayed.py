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
        # If BAR_SEC==30, approximate by splitting each 1m bar into two identical 30s bars
        if self.bar_sec == 30 and candles:
            split: List[Candle] = []
            for c in candles:
                split.append(c)
                split.append(Candle(
                    ticker=c.ticker,
                    ts=c.ts,  # keep same ts; downstream uses ordering not exact spacing
                    o=c.o, h=c.h, l=c.l, c=c.c, v=max(c.v // 2, 0), spread_est=c.spread_est
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
                    "indicators": {},
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
            "current_price": md.get("current_price", 0.0)
        }

    def get_market_data_summary(self) -> Dict[str, Dict]:
        return {t: {"current_price": md.get("current_price"), "indicators": md.get("indicators"), "last_update": md.get("last_update")} for t, md in self.market_data.items()}


