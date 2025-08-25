"""
EOD 리포터
- 미국 정규장 마감 직후(ET 16:05) 하루 요약을 Redis+파일로 저장
- KST 아침(08:05)에는 요약을 로그로만 출력 (Slack 비사용)
"""

from __future__ import annotations

import os
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional

import redis

logger = logging.getLogger(__name__)


def _get_redis() -> Optional[redis.Redis]:
    try:
        rurl = os.getenv("REDIS_URL", "redis://redis:6379/0")
        return redis.from_url(rurl)
    except Exception as e:
        logger.warning(f"EOD 리포터 Redis 연결 실패: {e}")
        return None


def _xr_count(r: redis.Redis, stream: str, start_ms: int, end_ms: int) -> int:
    try:
        # XRANGE stream start end COUNT N — 대략 상한선으로 충분히 큰 값 사용
        start_id = f"{start_ms}-0"
        end_id = f"{end_ms}-9999"
        # 대용량 방지: 페이지 단위로 합산
        total = 0
        last_id = start_id
        page = r.xrange(stream, min=start_id, max=end_id, count=1000)
        while page:
            total += len(page)
            last = page[-1][0]
            # 다음 페이지 시작 ID (exclusive)
            page = r.xrange(stream, min=last, max=end_id, count=1000)
            # 첫 항목이 last와 같을 수 있어 무한루프 방지
            if page and page[0][0] == last:
                page = page[1:]
        return total
    except Exception:
        return 0


def _build_eod_summary(now_utc: datetime | None = None) -> Dict[str, Any]:
    now_utc = now_utc or datetime.now(timezone.utc)
    et = timezone(timedelta(hours=-5))  # EST 기준, 세부 DST는 단순화
    kst = timezone(timedelta(hours=+9))

    # 리포팅 윈도우: 전일 16:00 ET ~ 당일 16:00 ET
    et_today = now_utc.astimezone(et).date()
    et_close = datetime.combine(et_today, datetime.min.time(), tzinfo=et).replace(hour=16)
    et_open = et_close - timedelta(hours=24)

    start_ms = int(et_open.timestamp() * 1000)
    end_ms = int(et_close.timestamp() * 1000)

    r = _get_redis()
    counts = {}
    if r:
        counts = {
            "signals_raw": _xr_count(r, "signals.raw", start_ms, end_ms),
            "signals_tradable": _xr_count(r, "signals.tradable", start_ms, end_ms),
            "orders_submitted": _xr_count(r, "orders.submitted", start_ms, end_ms),
            "orders_fills": _xr_count(r, "orders.fills", start_ms, end_ms),
            "risk_updates": _xr_count(r, "risk.pnl", start_ms, end_ms),
        }
    else:
        counts = {k: 0 for k in ["signals_raw", "signals_tradable", "orders_submitted", "orders_fills", "risk_updates"]}

    # 포트폴리오 요약(가능하면)
    portfolio: Dict[str, Any] = {}
    try:
        from app.adapters.trading_adapter import get_trading_adapter
        ta = get_trading_adapter()
        portfolio = ta.get_portfolio_summary() or {}
        portfolio["positions"] = portfolio.get("positions", [])
    except Exception as e:
        logger.warning(f"포트폴리오 요약 실패: {e}")

    summary = {
        "window_et": {
            "start": et_open.isoformat(),
            "end": et_close.isoformat(),
        },
        "generated_at": now_utc.astimezone(kst).isoformat(),
        "counts": counts,
        "portfolio": {
            "equity": portfolio.get("equity"),
            "positions_count": portfolio.get("positions_count", len(portfolio.get("positions", []))),
            "total_unrealized_pnl": portfolio.get("total_unrealized_pnl"),
        },
    }

    return summary


def write_eod_summary(summary: Dict[str, Any]) -> Optional[str]:
    """파일과 Redis에 저장"""
    try:
        # 파일 저장
        date_et = summary.get("window_et", {}).get("end", "").split("T")[0] or datetime.utcnow().strftime("%Y-%m-%d")
        ymd = date_et.replace("-", "")
        out_dir = os.path.join("/app", "logs", "eod")
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"{ymd}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)

        # Redis 저장
        r = _get_redis()
        if r:
            r.set(f"reports:eod:{ymd}", json.dumps(summary, ensure_ascii=False))
            r.set("reports:eod:last", ymd)
        return path
    except Exception as e:
        logger.error(f"EOD 요약 저장 실패: {e}")
        return None


# Celery 등록
try:
    from app.jobs.scheduler import celery_app
    CELERY_AVAILABLE = True
except Exception:
    celery_app = None
    CELERY_AVAILABLE = False


if CELERY_AVAILABLE and celery_app:
    @celery_app.task(name="app.jobs.eod_reporter.generate_eod_report")
    def generate_eod_report():
        try:
            summary = _build_eod_summary()
            path = write_eod_summary(summary)
            logger.info(f"EOD 보고서 생성 완료: {path}")
            return {"status": "ok", "path": path}
        except Exception as e:
            logger.error(f"EOD 보고서 생성 실패: {e}")
            return {"status": "error", "error": str(e)}

    @celery_app.task(name="app.jobs.eod_reporter.log_morning_kst")
    def log_morning_kst():
        try:
            r = _get_redis()
            ymd = r.get("reports:eod:last").decode() if r and r.get("reports:eod:last") else None
            if r and ymd:
                raw = r.get(f"reports:eod:{ymd}")
                if raw:
                    logger.info(f"☀️ KST 아침 요약 {ymd}: {raw.decode()[:4000]}")
                    return {"status": "ok", "ymd": ymd}
            logger.info("KST 아침 요약 없음")
            return {"status": "empty"}
        except Exception as e:
            logger.error(f"KST 아침 로그 실패: {e}")
            return {"status": "error", "error": str(e)}


