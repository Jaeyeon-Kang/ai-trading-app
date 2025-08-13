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
import math

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


# Day4 additions
def compute_trade_metrics(candles: List[Candle], entry_px: float, exit_px: float, side: str, sl_px: Optional[float], tp_px: Optional[float]) -> Dict:
    """Compute MFE/MAE in basis points, hold_minutes, realized R.
    Simplified: walk bars between entry and exit and find best/worst excursion vs entry.
    """
    if not candles:
        return {"mfe_bp": 0, "mae_bp": 0, "hold_minutes": 0, "realized_r": 0.0}
    # find segment: assume candles sorted
    idx_start = 0
    idx_end = len(candles) - 1
    # best-effort: choose last bar <= exit_px timestamp unknown -> use all bars
    mfe_bp = 0.0
    mae_bp = 0.0
    for c in candles[idx_start:idx_end+1]:
        # consider intrabar extremes
        up = (c.h - entry_px) / max(entry_px, 1e-9)
        dn = (entry_px - c.l) / max(entry_px, 1e-9)
        if side == "buy":
            mfe_bp = max(mfe_bp, up * 10000.0)
            mae_bp = max(mae_bp, dn * 10000.0)
        else:
            # short: invert
            mfe_bp = max(mfe_bp, dn * 10000.0)
            mae_bp = max(mae_bp, up * 10000.0)
    # hold minutes rough: number of bars * bar_sec/60 (unknown bar_sec → assume 0.5m per bar)
    hold_minutes = int(round(len(candles[idx_start:idx_end+1]) * 0.5))
    # realized R: (exit-entry)/(|entry-tp| or |entry-sl|) depending on side
    if side == "buy":
        r_denom = None
        if tp_px:
            r_denom = abs(tp_px - entry_px)
        if sl_px:
            r_denom = min(r_denom, abs(entry_px - sl_px)) if r_denom else abs(entry_px - sl_px)
        realized_r = ((exit_px - entry_px) / r_denom) if r_denom and r_denom > 0 else 0.0
    else:
        r_denom = None
        if tp_px:
            r_denom = abs(entry_px - tp_px)
        if sl_px:
            r_denom = min(r_denom, abs(sl_px - entry_px)) if r_denom else abs(sl_px - entry_px)
        realized_r = ((entry_px - exit_px) / r_denom) if r_denom and r_denom > 0 else 0.0
    return {
        "mfe_bp": int(round(mfe_bp)),
        "mae_bp": int(round(mae_bp)),
        "hold_minutes": hold_minutes,
        "realized_r": float(realized_r),
    }


def estimate_slippage_bp(entry_quote: Optional[Dict], entry_px: float) -> int:
    """Estimate slippage in bp from spread proxy.
    entry_quote may include 'spread_bp'. If absent, fallback to 5 bp.
    """
    try:
        if entry_quote and entry_quote.get("spread_bp") is not None:
            # assume half-spread paid + small impact
            sp = float(entry_quote["spread_bp"]) * 0.5 + 3.0
            return int(max(0, round(sp)))
    except Exception:
        pass
    return 5


def build_equity_curve(trades: List[Dict]) -> Tuple[List[float], Dict]:
    """Build cumulative equity curve and drawdown stats (bp, rough).
    Returns (equity_series, {dd_max_bp, last}).
    """
    eq = []
    total = 0.0
    for t in trades:
        total += float(t.get("pnl_cash", 0.0))
        eq.append(total)
    if not eq:
        return [], {"dd_max_bp": 0, "last": 0.0}
    peak = -1e18
    dd_max = 0.0
    for v in eq:
        if v > peak:
            peak = v
        dd = (peak - v)
        dd_max = max(dd_max, dd)
    # convert to bp relative to equity peak scale (avoid zero)
    scale = max(abs(max(eq)), 1.0)
    dd_max_bp = int(round((dd_max / scale) * 10000.0))
    return eq, {"dd_max_bp": dd_max_bp, "last": eq[-1]}


