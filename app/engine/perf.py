"""
Performance simulator (OCO) and summary metrics.

Reads orders from PostgreSQL and simulates exits using delayed Yahoo 1m data
approximated to BAR_SEC (default 30s). Writes optional fills/trades and
returns summary suitable for Slack daily report.

Assumptions (Day3 light version):
- Only paper orders (orders_paper) are considered
- Side is 'buy' or 'sell' (short treated symmetrically)
- Prices in USD; pnl_cash computed naïvely: (exit - entry) * qty (short sign inverted)
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import psycopg2
from psycopg2.extras import RealDictCursor

from app.io.quotes_delayed import DelayedQuotesIngestor, Candle


@dataclass
class Order:
    id: int
    ts: datetime
    ticker: str
    side: str
    qty: int
    entry: float
    sl: Optional[float]
    tp: Optional[float]


def _fetch_orders(days: int) -> List[Order]:
    dsn = os.getenv("POSTGRES_URL") or os.getenv("DATABASE_URL")
    if not dsn:
        return []
    since = datetime.now(timezone.utc) - timedelta(days=days)
    sql = (
        "SELECT id, ts, ticker, side, qty, px_entry AS entry, sl, tp "
        "FROM orders_paper WHERE ts >= %s ORDER BY ts ASC"
    )
    rows: List[Order] = []
    with psycopg2.connect(dsn) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, (since,))
            for r in cur.fetchall():
                rows.append(
                    Order(
                        id=int(r["id"]),
                        ts=r["ts"],
                        ticker=str(r["ticker"]).upper(),
                        side=str(r["side"]).lower(),
                        qty=int(r["qty"]),
                        entry=float(r["entry"]),
                        sl=float(r["sl"]) if r["sl"] is not None else None,
                        tp=float(r["tp"]) if r["tp"] is not None else None,
                    )
                )
    return rows


def _first_exit(candles: List[Candle], entry_ts: datetime, side: str, entry: float, sl: Optional[float], tp: Optional[float]) -> Tuple[Optional[datetime], Optional[float], str]:
    # 5bp default slippage
    bp = 0.0005
    after = [c for c in candles if c.ts.replace(tzinfo=timezone.utc) >= entry_ts.replace(tzinfo=timezone.utc)]
    for c in after:
        o, h, l = c.o, c.h, c.l
        if side == "buy":
            # gap at open
            if tp and o >= tp:
                return c.ts, o * (1 - bp), "tp_gap"
            if sl and o <= sl:
                return c.ts, o * (1 - bp), "sl_gap"
            # intrabar
            if tp and h >= tp:
                return c.ts, tp * (1 - bp), "tp_hit"
            if sl and l <= sl:
                return c.ts, sl * (1 - bp), "sl_hit"
        else:  # sell/short
            if tp and o <= tp:
                return c.ts, o * (1 + bp), "tp_gap"
            if sl and o >= sl:
                return c.ts, o * (1 + bp), "sl_gap"
            if tp and l <= tp:
                return c.ts, tp * (1 + bp), "tp_hit"
            if sl and h >= sl:
                return c.ts, sl * (1 + bp), "sl_hit"
    return None, None, "no_exit"


def simulate_and_summarize(days: int = 1, write_trades: bool = False) -> Dict:
    orders = _fetch_orders(days)
    if not orders:
        return {"summary": {"trades": 0}}

    ing = DelayedQuotesIngestor()
    ing.update_all_tickers()

    results = []
    wins = []
    losses = []
    total_pnl = 0.0
    holds = []
    for o in orders:
        candles = ing.get_latest_candles(o.ticker, 400)
        if not candles:
            continue
        exit_ts, exit_px, reason = _first_exit(candles, o.ts, o.side, o.entry, o.sl, o.tp)
        if not exit_px:
            continue
        hold_min = int((exit_ts - o.ts).total_seconds() / 60)
        sign = 1 if o.side == "buy" else -1
        pnl = (exit_px - o.entry) * o.qty * sign
        total_pnl += pnl
        holds.append(hold_min)
        if pnl >= 0:
            wins.append(pnl)
        else:
            losses.append(abs(pnl))
        results.append({
            "order_id": o.id,
            "ticker": o.ticker,
            "side": o.side,
            "entry": o.entry,
            "exit": exit_px,
            "qty": o.qty,
            "pnl_cash": pnl,
            "hold_minutes": hold_min,
            "exit_reason": reason,
        })

    trades = len(results)
    winrate = (len([r for r in results if r["pnl_cash"] >= 0]) / trades) * 100 if trades else 0.0
    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0
    pf = (sum(wins) / sum(losses)) if losses else (float("inf") if wins else 0.0)
    avg_hold = (sum(holds) / len(holds)) if holds else 0.0

    summary = {
        "trades": trades,
        "winrate": round(winrate, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "pf": round(pf, 2) if pf != float("inf") else "inf",
        "pnl_cash": round(total_pnl, 2),
        "avg_hold_minutes": round(avg_hold, 1),
        "slippage_bp": 5,  # fixed estimate in this light version
    }

    return {"summary": summary, "trades": results}


