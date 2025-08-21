"""
Celery beat 스케줄러
15-30초 주기로 시그널 생성 및 거래 실행
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import hashlib
import json
import logging
import os
import time
import math
# from decimal import Decimal  # 사용되지 않음
from typing import Any, Dict, List, Optional, Tuple, Union
import urllib.parse as _urlparse

import psycopg2
import redis

from celery import Celery
from celery.schedules import crontab
from celery.signals import beat_init, task_prerun, worker_process_init, worker_ready

# 로깅 설정 (다른 import보다 먼저)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

from app.config import settings, get_signal_cutoffs, sanitize_cutoffs_in_redis
from app.utils.rate_limiter import get_rate_limiter, TokenTier

# GPT-5 리스크 관리 통합
try:
    from app.engine.risk_manager import get_risk_manager
    from app.adapters.trading_adapter import get_trading_adapter
    RISK_MANAGER_AVAILABLE = True
except ImportError:
    RISK_MANAGER_AVAILABLE = False
    logger.warning("⚠️ 리스크 관리자 import 실패 - 기본 모드로 동작")

def check_signal_risk_feasibility(signal, session_label):
    """
    신호 발행 전 리스크 체크 (GPT-5 권장)
    동시위험 한도를 초과할 가능성이 있는 신호는 사전 필터링
    
    Returns:
        Tuple[bool, str]: (허용 여부, 사유)
    """
    if not RISK_MANAGER_AVAILABLE:
        return True, "리스크 관리자 비활성화"
    
    try:
        # 현재 포트폴리오 상태 확인
        trading_adapter = get_trading_adapter()
        portfolio = trading_adapter.get_portfolio_summary()
        positions = trading_adapter.get_positions()
        
        # 현재 총 위험 계산
        equity = portfolio.get('equity', 0)
        if equity <= 0:
            return False, "계좌 자산 확인 불가"
        
        current_risk = 0
        for pos in positions:
            # 각 포지션 위험 (1.5% 손절 가정)
            stop_distance = pos.avg_price * 0.015
            pos_risk = abs(pos.quantity * stop_distance) / equity
            current_risk += pos_risk
        
        # 신규 신호의 예상 위험 (0.5% 기본값)
        risk_manager = get_risk_manager()
        expected_new_risk = risk_manager.config.risk_per_trade
        
        # 동시위험 한도 체크 (2%)
        total_risk = current_risk + expected_new_risk
        max_risk = risk_manager.config.max_concurrent_risk
        
        if total_risk > max_risk:
            return False, f"동시위험 한도 초과 예상: {total_risk:.2%} > {max_risk:.2%}"
        
        # 포지션 수 체크
        if len(positions) >= risk_manager.config.max_positions:
            return False, f"최대 포지션 수 도달: {len(positions)}/{risk_manager.config.max_positions}"
        
        logger.info(f"✅ {signal.ticker} 리스크 pre-check 통과: 현재 {current_risk:.2%} + 예상 {expected_new_risk:.2%} = {total_risk:.2%}")
        return True, f"리스크 허용: {total_risk:.2%}/{max_risk:.2%}"
        
    except Exception as e:
        logger.error(f"❌ 리스크 pre-check 실패: {e}")
        # 안전을 위해 체크 실패시에도 허용 (기존 동작 유지)
        return True, "리스크 체크 오류로 기본 허용"

def check_signal_risk_feasibility_detailed(signal_data):
    """
    Redis 스트림 신호 데이터에 대한 리스크 체크
    
    Args:
        signal_data: Redis 스트림에서 가져온 신호 딕셔너리
        
    Returns:
        Tuple[bool, str]: (허용 여부, 사유)
    """
    if not RISK_MANAGER_AVAILABLE:
        return True, "리스크 관리자 비활성화"
    
    try:
        ticker = signal_data.get("ticker")
        entry_price = float(signal_data.get("entry_price", 0))
        stop_loss = float(signal_data.get("stop_loss", 0))
        
        if not ticker or entry_price <= 0 or stop_loss <= 0:
            return False, "신호 데이터 부족"
        
        # 현재 포트폴리오 상태 확인
        from app.adapters.trading_adapter import get_trading_adapter
        trading_adapter = get_trading_adapter()
        
        # 기본 리스크 체크
        risk_per_trade = abs(entry_price - stop_loss) / entry_price
        if risk_per_trade > 0.03:  # 3% 초과 손실 위험
            return False, f"단일 거래 위험 과대: {risk_per_trade:.2%} > 3%"
        
        # 포지션 수 체크 (간단 버전)
        try:
            positions = trading_adapter.get_positions()
            if len(positions) >= 5:  # 최대 5개 포지션
                return False, f"최대 포지션 수 도달: {len(positions)}/5"
        except Exception:
            pass  # 포지션 체크 실패해도 진행
        
        logger.info(f"✅ {ticker} 상세 리스크 체크 통과: 예상손실 {risk_per_trade:.2%}")
        return True, f"리스크 허용: 예상손실 {risk_per_trade:.2%}"
        
    except Exception as e:
        logger.error(f"❌ 상세 리스크 체크 실패: {e}")
        # 안전을 위해 체크 실패시에도 허용
        return True, "리스크 체크 오류로 기본 허용"

# Celery 앱 생성
celery_app = Celery(
    "trading_bot",
    broker=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
    backend=os.getenv("REDIS_URL", "redis://localhost:6379/0")
)

# 설정
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Seoul",  # KST
    enable_utc=True,        # UTC 유지 + 타임존-aware 스케줄 OK
    task_track_started=True,
    task_time_limit=30 * 60,  # 30분
    task_soft_time_limit=25 * 60,  # 25분
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=1000,
    include=[
        "app.jobs.scheduler",
        "app.jobs.paper_trading_manager",
        "app.jobs.daily_briefing",
    ],
)

# 스케줄 설정
celery_app.conf.beat_schedule = {
    # 15초마다 파이프라인 실행 (E2E)
    "pipeline-e2e": {
        "task": "app.jobs.scheduler.pipeline_e2e",
        "schedule": 15.0,  # 15초
    },
    # 15초마다 시그널 생성
    "generate-signals": {
        "task": "app.jobs.scheduler.generate_signals",
        "schedule": 15.0,  # 15초
    },
    # 30초마다 시세 업데이트
    "update-quotes": {
        "task": "app.jobs.scheduler.update_quotes",
        "schedule": 30.0,  # 30초
    },
    # 1분마다 EDGAR 스캔
    "scan-edgar": {
        "task": "app.jobs.scheduler.scan_edgar",
        "schedule": 60.0,  # 1분
    },
    # 5분마다 리스크 체크
    "check-risk": {
        "task": "app.jobs.scheduler.check_risk",
        "schedule": 300.0,  # 5분
    },
    # 매일 자정에 일일 리셋
    "daily-reset": {
        "task": "app.jobs.scheduler.daily_reset",
        "schedule": crontab(hour=0, minute=0),  # 매일 00:00
    },
    # 매일 06:10 KST에 일일 리포트 (KST = UTC+9, 06:10 KST = 21:10 UTC 전날)
    "daily-report": {
        "task": "app.jobs.scheduler.daily_report",
        "schedule": crontab(hour=21, minute=10),  # 매일 21:10 UTC = 06:10 KST
        "args": [False, True],  # force=False, post=True (슬랙으로 보내기)
    },
    # 5초마다 EDGAR 스트림 수집 → DB 적재 (dedupe는 DB UNIQUE와 ON CONFLICT로 보강)
    "ingest-edgar": {
        "task": "app.jobs.scheduler.ingest_edgar_stream",
        "schedule": 5.0,
    },
    # 15분마다 유니버스 재적용 (권장 분리)
    "refresh-universe": {
        "task": "app.jobs.scheduler.refresh_universe",
        "schedule": 900.0,
    },
    # 매일 05:55 KST 적응형 컷오프 갱신 (리포트 직전)
    "adaptive-cutoff": {
        "task": "app.jobs.scheduler.adaptive_cutoff",
        "schedule": crontab(hour=20, minute=55),  # 05:55 KST = 20:55 UTC 전날
    },
    
    # =============================================================================
    # Phase 1.5: 일일 브리핑 시스템 (답답함 해소!)
    # =============================================================================
    
    # 아침 브리핑 (09:00 KST = 00:00 UTC)
    "morning-briefing": {
        "task": "app.jobs.daily_briefing.send_scheduled_briefing",
        "schedule": crontab(hour=0, minute=0),  # 09:00 KST
        "args": ["morning"],
    },
    
    # 점심 브리핑 (12:30 KST = 03:30 UTC)  
    "midday-briefing": {
        "task": "app.jobs.daily_briefing.send_scheduled_briefing",
        "schedule": crontab(hour=3, minute=30),  # 12:30 KST
        "args": ["midday"],
    },
    
    # 저녁 브리핑 (18:00 KST = 09:00 UTC)
    "evening-briefing": {
        "task": "app.jobs.daily_briefing.send_scheduled_briefing",
        "schedule": crontab(hour=9, minute=0),  # 18:00 KST
        "args": ["evening"],
    },
    
    # 조용한 시장 체크 (1시간마다, 9-18시 KST만)
    "quiet-market-check": {
        "task": "app.jobs.daily_briefing.check_and_send_quiet_message",
        "schedule": crontab(minute=0),  # 매 정시마다
    },
    
    # =============================================================================
    # Phase 1.5: 스마트 페이퍼 트레이딩 시스템
    # =============================================================================
    
    # 스톱로스/익절 주문 체크 (5분마다)
    "check-stop-orders": {
        "task": "app.jobs.paper_trading_manager.check_stop_orders",
        "schedule": crontab(minute="*/5"),  # 5분마다
    },
    
    # 일일 성과 리포트 (저녁 19:00 KST = 10:00 UTC)
    "daily-paper-report": {
        "task": "app.jobs.paper_trading_manager.send_daily_report", 
        "schedule": crontab(hour=10, minute=0),  # 19:00 KST
    },
    # 마감 전 사전 예약 청산 (미 동부시간 15:48)
    "queue-preclose-liquidation": {
        "task": "app.jobs.scheduler.queue_preclose_liquidation",
        "schedule": crontab(hour=15, minute=48, tz='America/New_York'),
    },
    # 타임 스톱 가드레일 (1분마다)
    "enforce-time-stop": {
        "task": "app.jobs.scheduler.enforce_time_stop",
        "schedule": 60.0,
    },
    # 트레일 스톱 가드레일 (1분마다)
    "enforce-trail-stop": {
        "task": "app.jobs.scheduler.enforce_trail_stop",
        "schedule": 60.0,
    },
    # 역ETF 레짐 플립 가드레일 (1분마다)
    "enforce-regime-flatten-inverse": {
        "task": "app.jobs.scheduler.enforce_regime_flatten_inverse",
        "schedule": 60.0,
    },
}

# 전역 변수 (실제로는 의존성 주입 사용)
trading_components = {
    "quotes_ingestor": None,
    "edgar_scanner": None,
    "regime_detector": None,
    "tech_score_engine": None,
    "llm_engine": None,
    "signal_mixer": None,
    "risk_engine": None,
    "paper_ledger": None,
    "redis_streams": None,
    "slack_bot": None,
    "stream_consumer": None,
    "db_connection": None,
}

def _parse_redis_url(url: str) -> tuple[str, int, int]:
    try:
        parsed = _urlparse.urlparse(url)
        host = parsed.hostname or "redis"
        port = int(parsed.port or 6379)
        db = int((parsed.path or "/0").lstrip("/") or 0)
        return host, port, db
    except Exception:
        return "redis", 6379, 0

def _autoinit_components_if_enabled() -> None:
    """Best-effort 컴포넌트 자동 초기화 (워커/비트 프로세스 기동 시).
    필수 구성만이라도 준비해 components_not_ready 스킵을 줄인다.
    """
    if os.getenv("AUTO_INIT_COMPONENTS", "true").lower() not in ("1","true","yes","on"):  # opt-out
        return
    try:
        from app.io.quotes_delayed import DelayedQuotesIngestor
        from app.engine.regime import RegimeDetector
        from app.engine.techscore import TechScoreEngine
        from app.engine.mixer import SignalMixer
        from app.engine.risk import RiskEngine
        from app.adapters.paper_ledger import PaperLedger
        from app.io.streams import RedisStreams, StreamConsumer
        from app.engine.llm_insight import LLMInsightEngine
        from app.io.slack_bot import SlackBot
    except Exception as e:
        logger.warning(f"컴포넌트 임포트 실패: {e}")
        return

    components: Dict[str, object] = {}

    # Quotes ingestor
    try:
        components["quotes_ingestor"] = DelayedQuotesIngestor()
        # 워밍업으로 빈 버퍼 방지
        components["quotes_ingestor"].warmup_backfill()  # type: ignore
        logger.info("딜레이드 Quotes 인제스터 워밍업 완료")
    except Exception as e:
        logger.warning(f"Quotes 인제스터 준비 실패: {e}")

    # Regime/Tech/Mixer
    try:
        components["regime_detector"] = RegimeDetector()
    except Exception as e:
        logger.warning(f"RegimeDetector 준비 실패: {e}")
    try:
        components["tech_score_engine"] = TechScoreEngine()
    except Exception as e:
        logger.warning(f"TechScoreEngine 준비 실패: {e}")
    try:
        thr = settings.MIXER_THRESHOLD
        components["signal_mixer"] = SignalMixer(buy_threshold=thr, sell_threshold=-thr)
    except Exception as e:
        logger.warning(f"SignalMixer 준비 실패: {e}")

    # Risk/Paper ledger
    try:
        init_cap = float(os.getenv("INITIAL_CAPITAL", "1000000"))
        components["risk_engine"] = RiskEngine(initial_capital=init_cap)
    except Exception as e:
        logger.warning(f"RiskEngine 준비 실패: {e}")
    try:
        components["paper_ledger"] = PaperLedger(initial_cash=float(os.getenv("INITIAL_CAPITAL", "1000000")))
    except Exception as e:
        logger.warning(f"PaperLedger 준비 실패: {e}")

    # Redis streams / consumer
    try:
        rurl = os.getenv("REDIS_URL", "redis://redis:6379/0")
        host, port, db = _parse_redis_url(rurl)
        rs = RedisStreams(host=host, port=port, db=db)
        components["redis_streams"] = rs
        components["stream_consumer"] = StreamConsumer(rs)
    except Exception as e:
        logger.warning(f"Redis Streams 준비 실패: {e}")

    # Slack bot (optional)
    try:
        token = os.getenv("SLACK_BOT_TOKEN")
        channel = os.getenv("SLACK_CHANNEL_ID") or os.getenv("SLACK_CHANNEL", "#trading-signals")
        logger.info(f"🔍 [DEBUG] SlackBot 환경변수: token={'***' if token else None}, channel_id={os.getenv('SLACK_CHANNEL_ID')}, channel_fallback={os.getenv('SLACK_CHANNEL')}")
        logger.info(f"🔍 [DEBUG] SlackBot 최종 채널: {channel}")
        if token:
            components["slack_bot"] = SlackBot(token=token, channel=channel)
            logger.info(f"Slack 봇 준비: 채널 {channel}")
        else:
            logger.warning("Slack 토큰 없음 - 슬랙 비활성")
    except Exception as e:
        logger.warning(f"SlackBot 준비 실패: {e}")

    # LLM (optional)
    try:
        llm = LLMInsightEngine()
        if components.get("slack_bot"):
            llm.set_slack_bot(components["slack_bot"])  # type: ignore
        components["llm_engine"] = llm
    except Exception as e:
        logger.warning(f"LLM 엔진 준비 실패: {e}")

    if components:
        try:
            initialize_components(components)  # 기존 헬퍼로 주입
        except Exception as e:
            logger.warning(f"컴포넌트 주입 실패: {e}")

# Celery 신호에 자동 초기화 연결
@worker_ready.connect
def _on_worker_ready(sender=None, **kwargs):
    _autoinit_components_if_enabled()
    # 컷오프 정화 및 로깅
    result = sanitize_cutoffs_in_redis()
    if result:
        # 컷오프 정합성 체크
        mixer_thr = settings.MIXER_THRESHOLD
        rth_cut = result['rth']
        if rth_cut > mixer_thr:
            logger.warning(f"⚠️ RTH cutoff({rth_cut}) > MIXER({mixer_thr}) : 재검토 필요")
        logger.info(f"컷오프 정합성: mixer={mixer_thr}, rth={rth_cut}, delta={rth_cut - mixer_thr:.3f}")
        logger.info(f"컷오프 로드됨: RTH={result['rth']:.3f}, EXT={result['ext']:.3f}")
    else:
        rth, ext = get_signal_cutoffs()
        logger.info(f"컷오프 로드됨: RTH={rth:.3f}, EXT={ext:.3f}")

@beat_init.connect
def _on_beat_init(sender=None, **kwargs):
    _autoinit_components_if_enabled()
    # 컷오프 정화 및 로깅
    result = sanitize_cutoffs_in_redis()
    if result:
        logger.info(f"컷오프 로드됨: RTH={result['rth']:.3f}, EXT={result['ext']:.3f}")
    else:
        rth, ext = get_signal_cutoffs()
        logger.info(f"컷오프 로드됨: RTH={rth:.3f}, EXT={ext:.3f}")

# 각 워커 프로세스(prefork)에서 한 번씩 초기화
@worker_process_init.connect
def _on_worker_process_init(sender=None, **kwargs):
    _autoinit_components_if_enabled()
    # 컷오프 정화 및 로깅
    result = sanitize_cutoffs_in_redis()
    if result:
        logger.info(f"컷오프 로드됨: RTH={result['rth']:.3f}, EXT={result['ext']:.3f}")
    else:
        rth, ext = get_signal_cutoffs()
        logger.info(f"컷오프 로드됨: RTH={rth:.3f}, EXT={ext:.3f}")

# 태스크 직전에도 필수 컴포넌트가 없으면 마지막 시도
@task_prerun.connect
def _ensure_components_before_task(task=None, **kwargs):
    try:
        required_keys = [
            "quotes_ingestor",
            "regime_detector",
            "tech_score_engine",
            "signal_mixer",
        ]
        missing = [k for k in required_keys if not trading_components.get(k)]
        if missing:
            logger.warning(f"필수 컴포넌트 미준비(prerun): {missing} → 자동 초기화 시도")
            _autoinit_components_if_enabled()
    except Exception as e:
        logger.warning(f"prerun 자동 초기화 실패: {e}")

# =============================================================================
# Tier System Functions (Universe Expansion)
# =============================================================================

def get_ticker_tier(ticker: str) -> Optional[TokenTier]:
    """
    종목이 속한 Tier 반환
    
    Args:
        ticker: 종목 코드
        
    Returns:
        Optional[TokenTier]: Tier 타입 (None이면 벤치)
    """
    ticker = ticker.upper().strip()
    
    tier_a_tickers = [t.strip().upper() for t in settings.TIER_A_TICKERS]
    tier_b_tickers = [t.strip().upper() for t in settings.TIER_B_TICKERS]
    
    if ticker in tier_a_tickers:
        return TokenTier.TIER_A
    elif ticker in tier_b_tickers:
        return TokenTier.TIER_B
    else:
        return None  # 벤치 (이벤트 기반)

def should_process_ticker_now(ticker: str, current_time: Optional[Union[datetime, int]] = None) -> Tuple[bool, str]:
    """
    현재 시간에 해당 종목을 분석해야 하는지 판단
    
    Args:
        ticker: 종목 코드
        current_time: 현재 시간 (None이면 datetime.now())
        
    Returns:
        Tuple[bool, str]: (처리 여부, 사유)
    """
    if current_time is None:
        current_time = datetime.now()
    
    tier = get_ticker_tier(ticker)
    
    # 벤치 종목은 이벤트 기반만 처리 (현재는 스킵)
    if tier is None:
        return False, "bench_event_only"
    
    # 현재 분의 초 (timestamp에서 datetime 객체로 변환)
    if isinstance(current_time, int):
        from datetime import datetime
        current_time = datetime.fromtimestamp(current_time)
    current_second = current_time.second
    
    if tier == TokenTier.TIER_A:
        # Tier A: 30초마다 (0초, 30초)
        return current_second % 30 == 0, "tier_a_schedule"
    elif tier == TokenTier.TIER_B:
        # Tier B: 60초마다 (0초만)
        return current_second == 0, "tier_b_schedule"
    
    return False, "unknown_tier"

def can_consume_api_token_for_ticker(ticker: str) -> Tuple[bool, str]:
    """
    해당 종목 분석을 위한 API 토큰 소비 가능 여부 확인
    
    Args:
        ticker: 종목 코드
        
    Returns:
        Tuple[bool, str]: (소비 가능 여부, 사유)
    """
    try:
        rate_limiter = get_rate_limiter()
        tier = get_ticker_tier(ticker)
        
        if tier is None:
            # 벤치 종목은 예약 토큰 사용
            tier = TokenTier.RESERVE
        
        can_consume = rate_limiter.can_consume_token(tier)
        if can_consume:
            return True, f"token_available_{tier.value}"
        else:
            return False, f"token_exhausted_{tier.value}"
            
    except Exception as e:
        logger.error(f"토큰 확인 실패: {e}")
        return False, f"token_check_error: {e}"

def consume_api_token_for_ticker(ticker: str) -> Tuple[bool, str]:
    """
    해당 종목 분석을 위한 API 토큰 실제 소비
    
    Args:
        ticker: 종목 코드
        
    Returns:
        Tuple[bool, str]: (소비 성공 여부, 사유)
    """
    try:
        rate_limiter = get_rate_limiter()
        tier = get_ticker_tier(ticker)
        
        if tier is None:
            # 벤치 종목은 예약 토큰 사용 (Fallback도 시도)
            success, used_tier = rate_limiter.try_consume_with_fallback(
                TokenTier.RESERVE, 
                TokenTier.TIER_B  # Reserve 부족시 Tier B 사용
            )
            return success, f"consumed_{used_tier.value}_for_bench"
        else:
            # Tier A/B는 해당 토큰 사용.
            # 테스트 한정: 분 초 0–10s 구간에만 Tier A → Reserve 폴백 허용(그 외엔 폴백 금지)
            fallback_tier = None
            if tier == TokenTier.TIER_A:
                from datetime import datetime
                now = datetime.utcnow()
                if 0 <= now.second <= 10:
                    fallback_tier = TokenTier.RESERVE
            success, used_tier = rate_limiter.try_consume_with_fallback(tier, fallback_tier)
            return success, f"consumed_{used_tier.value}"
            
    except Exception as e:
        logger.error(f"토큰 소비 실패: {e}")
        return False, f"token_consume_error: {e}"

def get_universe_with_tiers() -> Dict[str, List[str]]:
    """
    Tier별 종목 리스트 반환
    
    Returns:
        Dict[str, List[str]]: Tier별 종목 딕셔너리
    """
    try:
        # 동적 유니버스 조회 (기존 로직 재사용)
        dynamic_universe = None
        try:
            rurl = os.getenv("REDIS_URL")
            if rurl:
                r = redis.from_url(rurl)
                external = r.smembers("universe:external") or []
                watch = r.smembers("universe:watchlist") or []
                core = [t.strip().upper() for t in (os.getenv("TICKERS", "").split(",")) if t.strip()]
                ext = [x.decode() if isinstance(x, (bytes, bytearray)) else x for x in external]
                wch = [x.decode() if isinstance(x, (bytes, bytearray)) else x for x in watch]
                merged = []
                seen = set()
                max_n = int(os.getenv("UNIVERSE_MAX", "100"))
                for arr in [core, ext, wch]:
                    for s in arr:
                        if s and s not in seen:
                            merged.append(s)
                            seen.add(s)
                        if len(merged) >= max_n:
                            break
                    if len(merged) >= max_n:
                        break
                dynamic_universe = merged
        except Exception:
            dynamic_universe = None
        
        # Fallback: 설정에서 직접 가져오기
        all_tickers = dynamic_universe or []
        if not all_tickers:
            all_tickers = (
                settings.TIER_A_TICKERS + 
                settings.TIER_B_TICKERS + 
                settings.BENCH_TICKERS
            )
        
        # Tier별 분류
        tier_a = [t for t in all_tickers if get_ticker_tier(t) == TokenTier.TIER_A]
        tier_b = [t for t in all_tickers if get_ticker_tier(t) == TokenTier.TIER_B] 
        bench = [t for t in all_tickers if get_ticker_tier(t) is None]
        
        return {
            "tier_a": tier_a,
            "tier_b": tier_b,
            "bench": bench
        }
        
    except Exception as e:
        logger.error(f"유니버스 Tier 분류 실패: {e}")
        return {
            "tier_a": settings.TIER_A_TICKERS,
            "tier_b": settings.TIER_B_TICKERS,
            "bench": settings.BENCH_TICKERS
        }

def should_call_llm_for_event(ticker: str, event_type: str, signal_score: float = None, 
                              edgar_filing: Dict = None) -> Tuple[bool, str]:
    """
    LLM 호출 게이팅 시스템 - 엄격한 이벤트 기반 트리거링
    
    Args:
        ticker: 종목 코드
        event_type: 이벤트 타입 ('edgar', 'vol_spike')
        signal_score: 신호 점수 (vol_spike용)
        edgar_filing: EDGAR 공시 정보
        
    Returns:
        Tuple[bool, str]: (호출 허용 여부, 사유)
    """
    try:
        # 테스트용 RTH 강제 플래그
        TEST_FORCE_RTH = os.getenv("TEST_FORCE_RTH", "0").lower() in ("1", "true", "on")
        
        # LLM 게이팅 비활성화시 무조건 허용
        if not settings.LLM_GATING_ENABLED:
            return True, "gating_disabled"
        
        # 1. 일일 호출 한도 체크
        rurl = os.getenv("REDIS_URL")
        if rurl:
            r = redis.from_url(rurl)
            
            # ET 기준 날짜 키
            et_tz = timezone(timedelta(hours=-5))
            now_et = datetime.now(et_tz)
            if 3 <= now_et.month <= 11:  # DST 간이 적용
                et_tz = timezone(timedelta(hours=-4))
                now_et = datetime.now(et_tz)
            
            daily_key = f"llm_calls:{now_et:%Y%m%d}"
            current_calls = int(r.get(daily_key) or 0)
            
            if current_calls >= settings.LLM_DAILY_CALL_LIMIT:
                return False, f"daily_limit_exceeded ({current_calls}/{settings.LLM_DAILY_CALL_LIMIT})"
        
        # 2. 이벤트별 조건 검증
        if event_type == "edgar":
            # EDGAR 이벤트는 항상 허용 (중요도 높음)
            cache_key = f"llm_cache:edgar:{ticker}:{edgar_filing.get('form_type', 'unknown')}"
            
        elif event_type == "vol_spike":
            # vol_spike는 신호 점수 조건 필요
            if signal_score is None or abs(signal_score) < settings.LLM_MIN_SIGNAL_SCORE:
                logger.info(f"🤖 LLM gate: block {ticker}/{event_type} "
                           f"score={signal_score:.3f} < min={settings.LLM_MIN_SIGNAL_SCORE}")
                return False, "signal_score_too_low"
            
            cache_key = f"llm_cache:vol_spike:{ticker}"
            
        else:
            return False, f"unknown_event_type: {event_type}"
        
        # 3. 중복 방지 캐시 체크 (30분)
        if rurl:
            cached = r.get(cache_key)
            if cached:
                return False, f"cached_within_{settings.LLM_CACHE_DURATION_MIN}min"
        
        return True, f"allowed_{event_type}"
        
    except Exception as e:
        logger.error(f"LLM 게이팅 체크 실패: {e}")
        # 안전을 위해 에러시 차단
        return False, f"gating_error: {e}"

def consume_llm_call_quota(ticker: str, event_type: str, edgar_filing: Dict = None) -> bool:
    """
    LLM 호출 쿼터 실제 소비 및 캐시 마킹
    
    Args:
        ticker: 종목 코드
        event_type: 이벤트 타입
        edgar_filing: EDGAR 공시 정보
        
    Returns:
        bool: 소비 성공 여부
    """
    try:
        rurl = os.getenv("REDIS_URL")
        if not rurl:
            return True
        
        r = redis.from_url(rurl)
        
        # 일일 카운터 증가
        et_tz = timezone(timedelta(hours=-5))
        now_et = datetime.now(et_tz)
        if 3 <= now_et.month <= 11:
            et_tz = timezone(timedelta(hours=-4))
            now_et = datetime.now(et_tz)
        
        daily_key = f"llm_calls:{now_et:%Y%m%d}"
        r.incr(daily_key)
        r.expire(daily_key, 86400)  # 24시간 TTL
        
        # 캐시 마킹
        if event_type == "edgar":
            cache_key = f"llm_cache:edgar:{ticker}:{edgar_filing.get('form_type', 'unknown')}"
        else:
            cache_key = f"llm_cache:vol_spike:{ticker}"
        
        r.setex(cache_key, settings.LLM_CACHE_DURATION_MIN * 60, 1)
        
        return True
        
    except Exception as e:
        logger.error(f"LLM 쿼터 소비 실패: {e}")
        return False

def get_llm_usage_stats() -> Dict[str, int]:
    """
    LLM 사용량 통계 조회
    
    Returns:
        Dict: 사용량 통계
    """
    try:
        rurl = os.getenv("REDIS_URL")
        if not rurl:
            return {"daily_used": 0, "daily_limit": settings.LLM_DAILY_CALL_LIMIT}
        
        r = redis.from_url(rurl)
        
        et_tz = timezone(timedelta(hours=-5))
        now_et = datetime.now(et_tz)
        if 3 <= now_et.month <= 11:
            et_tz = timezone(timedelta(hours=-4))
            now_et = datetime.now(et_tz)
        
        daily_key = f"llm_calls:{now_et:%Y%m%d}"
        daily_used = int(r.get(daily_key) or 0)
        
        return {
            "daily_used": daily_used,
            "daily_limit": settings.LLM_DAILY_CALL_LIMIT,
            "remaining": max(0, settings.LLM_DAILY_CALL_LIMIT - daily_used),
            "usage_pct": (daily_used / settings.LLM_DAILY_CALL_LIMIT) * 100 if settings.LLM_DAILY_CALL_LIMIT > 0 else 0
        }
        
    except Exception as e:
        logger.error(f"LLM 사용량 조회 실패: {e}")
        return {"daily_used": 0, "daily_limit": settings.LLM_DAILY_CALL_LIMIT, "error": str(e)}

# ============================================================================
# 포지션 관리 및 리스크 계산 헬퍼 함수들
# ============================================================================

# 거래 파라미터 (환경변수에서 로드)
BUY_THRESHOLD = float(os.getenv("BUY_THRESHOLD", "0.15"))
SELL_THRESHOLD = float(os.getenv("SELL_THRESHOLD", "-0.15")) 
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "120"))
DIRECTION_LOCK_SECONDS = int(os.getenv("DIRECTION_LOCK_SECONDS", "90"))
RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "0.005"))  # 0.5%
MAX_CONCURRENT_RISK = float(os.getenv("MAX_CONCURRENT_RISK", "0.02"))  # 2%
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.015"))  # 1.5%
TAKE_PROFIT_RR = float(os.getenv("TAKE_PROFIT_RR", "1.5"))  # 1.5R
EOD_FLATTEN_MINUTES = int(os.getenv("EOD_FLATTEN_MINUTES", "10"))  # 기본 10분 전으로 확대
FRACTIONAL_ENABLED = os.getenv("FRACTIONAL_ENABLED", "0") == "1"

# 가드레일 플래그/파라미터
ENABLE_TIME_STOP = os.getenv("ENABLE_TIME_STOP", "1") in ("1", "true", "True")
TIME_STOP_MIN = int(os.getenv("TIME_STOP_MIN", "45"))
TIME_STOP_LATE_ENTRY_CUTOFF_MIN = int(os.getenv("TIME_STOP_LATE_ENTRY_CUTOFF_MIN", "10"))

ENABLE_TRAIL_STOP = os.getenv("ENABLE_TRAIL_STOP", "0") in ("1", "true", "True")
TRAIL_RET_PCT = float(os.getenv("TRAIL_RET_PCT", "0.005"))
TRAIL_MIN_HOLD_MIN = int(os.getenv("TRAIL_MIN_HOLD_MIN", "5"))

ENABLE_REGIME_FLATTEN_INVERSE = os.getenv("ENABLE_REGIME_FLATTEN_INVERSE", "1") in ("1", "true", "True")
INVERSE_TICKERS_ENV = os.getenv("INVERSE_TICKERS", "SOXS,SQQQ,SPXS,TZA,SDOW,TECS,DRV,SARK")
INVERSE_TICKERS_SET = set(t.strip().upper() for t in INVERSE_TICKERS_ENV.split(",") if t.strip())

# EOD 재시도 파라미터
EOD_MAX_RETRIES = int(os.getenv("EOD_MAX_RETRIES", "2"))
EOD_RETRY_DELAY_SEC = int(os.getenv("EOD_RETRY_DELAY_SEC", "30"))

# 숏 ETF 청산 로직 파라미터
MIN_HOLD_SEC = int(os.getenv("MIN_HOLD_SEC", "60"))  # 최소 보유시간 60초
TRAIL_R_RATIO = float(os.getenv("TRAIL_R_RATIO", "0.7"))  # 트레일링 스탑 비율 0.7R
VOLUME_SPIKE_MULTIPLIER = float(os.getenv("VOLUME_SPIKE_MULTIPLIER", "1.5"))  # 볼륨 스파이크 1.5배
CANDLE_BREAK_EPSILON = float(os.getenv("CANDLE_BREAK_EPSILON", "0.01"))  # 캔들 브레이크 임계값

# 인버스 ETF 메타데이터
INSTRUMENT_META = {
    "AAPL": {"underlying": "AAPL", "exposure_sign": +1, "min_qty": 1},
    "NVDA": {"underlying": "NVDA", "exposure_sign": +1, "min_qty": 1},
    "TSLA": {"underlying": "TSLA", "exposure_sign": +1, "min_qty": 1},
    "MSFT": {"underlying": "MSFT", "exposure_sign": +1, "min_qty": 1},
    "SQQQ": {"underlying": "QQQ", "exposure_sign": -1, "min_qty": 1},
    "SOXS": {"underlying": "SOXX", "exposure_sign": -1, "min_qty": 1},
    "SPXS": {"underlying": "SPY", "exposure_sign": -1, "min_qty": 1},
    "TZA": {"underlying": "IWM", "exposure_sign": -1, "min_qty": 1},
    "SDOW": {"underlying": "DIA", "exposure_sign": -1, "min_qty": 1},
    "TECS": {"underlying": "XLK", "exposure_sign": -1, "min_qty": 1},
    "DRV": {"underlying": "XLE", "exposure_sign": -1, "min_qty": 1},
    "SARK": {"underlying": "ARKK", "exposure_sign": -1, "min_qty": 1},
}

# 바스켓 기반 라우팅 시스템
BASKETS = {
    "MEGATECH": {
        "tickers": {"AAPL", "MSFT", "AMZN", "META", "GOOGL", "TSLA"},
        "etf": "SQQQ",  # NASDAQ-100 인버스
        "min_signals": 1,  # 최소 1개 종목에서 신호 
        "neg_fraction": 0.20,  # 20% 이상 음수 (적당히 완화)
        "mean_threshold": -0.05  # 평균 스코어 -0.05 이하
    },
    "SEMIS": {
        "tickers": {"NVDA", "AMD", "AVGO"},
        "etf": "SOXS",  # 반도체 인버스
        "min_signals": 1,  # 완화
        "neg_fraction": 0.25,  # 25% 이상 음수
        "mean_threshold": -0.05  # 평균 스코어 -0.05 이하
    }
}

# 인버스 ETF 목록
INVERSE_ETFS = {"SQQQ", "SOXS", "SPXS", "TZA", "SDOW", "TECS", "DRV", "SARK"}

# 상충 포지션 맵
CONFLICT_MAP = {
    "SQQQ": {"QQQ"},      # NASDAQ 롱/숏 동시 금지
    "SOXS": {"SOXL", "SOXX"},  # 반도체 롱/숏 동시 금지
    "SPXS": {"SPY"},      # S&P 500 롱/숏 동시 금지
    "TZA": {"IWM"},       # Russell 2000 롱/숏 동시 금지
    "SDOW": {"DIA"},      # Dow Jones 롱/숏 동시 금지
}

def get_basket_for_symbol(symbol: str) -> Optional[str]:
    """심볼이 속한 바스켓 찾기"""
    for basket_name, basket_info in BASKETS.items():
        if symbol in basket_info["tickers"]:
            return basket_name
    return None

def get_basket_state(basket_name: str, window_seconds: int = None) -> Dict[str, Any]:
    """
    바스켓 상태 집계 (signals:recent 리스트에서 최근 신호들 분석)
    
    Returns:
        {"neg_count": int, "total_count": int, "mean_score": float, 
         "neg_fraction": float, "two_tick": bool, "slope": float}
    """
    try:
        # 파라미터 명확화
        WINDOW_SEC = int(os.getenv("BASKET_WINDOW_SEC", "120"))
        SAMPLE_N = int(os.getenv("BASKET_SAMPLE_N", "200"))
        
        if window_seconds is None:
            window_seconds = WINDOW_SEC
            
        rurl = os.getenv("REDIS_URL")
        if not rurl:
            return {"neg_count": 0, "total_count": 0, "mean_score": 0, 
                   "neg_fraction": 0, "two_tick": False, "slope": 0}
        
        r = redis.from_url(rurl)
        basket_info = BASKETS.get(basket_name, {})
        tickers = basket_info.get("tickers", set())
        
        # signals:recent 리스트에서 최근 신호들 수집
        now = time.time()
        cutoff_time = now - window_seconds
        
        current_scores = []
        trend_scores = []  # slope 계산용
        
        # signals:recent 리스트에서 설정된 표본 수만큼 가져오기
        recent_signals = r.lrange("signals:recent", 0, SAMPLE_N - 1)
        
        for signal_data in recent_signals:
            try:
                signal_info = json.loads(signal_data)
                ticker = signal_info.get("ticker")
                score = signal_info.get("score", 0)
                timestamp_str = signal_info.get("timestamp", "")
                
                # 바스켓에 속한 종목만 처리
                if ticker not in tickers:
                    continue
                
                # 타임스탬프 파싱 (ISO 형식)
                if timestamp_str:
                    try:
                        from datetime import datetime
                        timestamp = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00')).timestamp()
                    except:
                        continue
                else:
                    continue
                
                # 현재 윈도우 내 신호만 사용
                if timestamp >= cutoff_time:
                    current_scores.append(score)
                    trend_scores.append((timestamp, score))
            except Exception as e:
                logger.debug(f"신호 파싱 실패: {e}")
                continue
        
        if not current_scores:
            return {"neg_count": 0, "total_count": 0, "mean_score": 0, 
                   "neg_fraction": 0, "two_tick": False, "slope": 0}
        
        neg_count = sum(1 for s in current_scores if s < -0.01)
        total_count = len(current_scores)
        mean_score = sum(current_scores) / len(current_scores) if current_scores else 0
        neg_fraction = neg_count / total_count if total_count > 0 else 0
        
        # 2틱 확인 (이전 윈도우와 비교)
        prev_key = f"basket_state:{basket_name}:prev"
        prev_data = r.get(prev_key)
        two_tick = False
        if prev_data:
            try:
                prev_state = json.loads(prev_data)
                if prev_state.get("neg_fraction", 0) >= 0.5 and neg_fraction >= 0.5:
                    two_tick = True
            except:
                pass
        
        # 현재 상태 저장 (다음 비교용)
        r.setex(prev_key, 120, json.dumps({
            "neg_fraction": neg_fraction,
            "mean_score": mean_score,
            "timestamp": now
        }))
        
        # 실제 slope 계산 (간단한 선형 회귀)
        slope = 0
        if len(trend_scores) >= 3:
            # 시간순 정렬
            trend_scores.sort(key=lambda x: x[0])
            
            # 간단한 기울기 계산: (마지막 - 처음) / 시간차이
            if len(trend_scores) >= 2:
                first_score = trend_scores[0][1]
                last_score = trend_scores[-1][1]
                time_diff = trend_scores[-1][0] - trend_scores[0][0]
                if time_diff > 0:
                    slope = (last_score - first_score) / time_diff
        
        return {
            "neg_count": neg_count,
            "total_count": total_count,
            "mean_score": mean_score,
            "neg_fraction": neg_fraction,
            "two_tick": two_tick,
            "slope": slope
        }
        
    except Exception as e:
        logger.error(f"바스켓 상태 집계 실패: {e}")
        return {"neg_count": 0, "total_count": 0, "mean_score": 0, 
               "neg_fraction": 0, "two_tick": False, "slope": 0}

def has_existing_position(symbol: str) -> bool:
    """기존 포지션 존재 여부 체크 (GPT 요구: 추가 매수 금지)"""
    try:
        from app.adapters.trading_adapter import get_trading_adapter
        trading_adapter = get_trading_adapter()
        positions = trading_adapter.get_positions()
        
        for pos in positions:
            if pos.ticker == symbol and pos.quantity > 0:
                return True
        return False
        
    except Exception as e:
        logger.warning(f"포지션 체크 실패: {e}")
        return False

def can_open_etf_position(etf_symbol: str) -> Tuple[bool, str]:
    """
    ETF 포지션 오픈 가능 여부 체크
    - ETF 락 체크
    - 기존 포지션 체크
    - 상충 포지션 체크
    """
    try:
        rurl = os.getenv("REDIS_URL")
        if not rurl:
            return True, "redis_not_available"
        
        r = redis.from_url(rurl)
        
        # 1. ETF 락 체크 (90초 TTL)
        lock_key = f"etf_lock:{etf_symbol}"
        if not r.setnx(lock_key, 1):
            return False, "etf_locked"
        r.expire(lock_key, 90)
        
        # 2. 상충 포지션 체크
        conflicts = CONFLICT_MAP.get(etf_symbol, set())
        for conflict_symbol in conflicts:
            if has_existing_position(conflict_symbol):
                r.delete(lock_key)  # 락 해제
                return False, f"conflict_with_{conflict_symbol}"
        
        return True, "ok"
        
    except Exception as e:
        logger.error(f"ETF 포지션 체크 실패: {e}")
        return False, f"error_{e}"

def route_signal_symbol(original_symbol: str, base_score: float) -> Dict[str, str]:
    """
    바스켓 기반 신호 라우팅 (개선된 버전)
    
    Args:
        original_symbol: 원래 신호 심볼
        base_score: 원래 신호 스코어
        
    Returns:
        라우팅 결과 딕셔너리
    """
    # 인버스 ETF는 라우팅하지 않음 (패스스루)
    if original_symbol in INVERSE_ETFS:
        return {
            "exec_symbol": original_symbol,
            "intent": "direct",
            "route_reason": "inverse_etf_passthrough"
        }
    
    # 롱 신호는 원래 심볼 그대로
    if base_score >= 0:
        return {
            "exec_symbol": original_symbol,
            "intent": "enter_long",
            "route_reason": f"{original_symbol}->자체(롱)"
        }
    
    # 숏 신호: 바스켓 체크
    basket_name = get_basket_for_symbol(original_symbol)
    if not basket_name:
        # 바스켓에 속하지 않는 종목은 스킵 (SPXS fallback 제거)
        return {
            "exec_symbol": None,
            "intent": "skip",
            "route_reason": "not_in_any_basket"
        }
    
    # 바스켓 상태 확인
    basket_state = get_basket_state(basket_name)
    basket_info = BASKETS[basket_name]
    
    # 바스켓 조건 체크 (테스트용 극도 완화)
    conditions_met = (
        basket_state["total_count"] >= basket_info.get("min_signals", 1) and
        basket_state["neg_fraction"] >= basket_info.get("neg_fraction", 0.0) and
        basket_state["mean_score"] <= basket_info.get("mean_threshold", 0.5)
        # 2틱 및 slope 조건 제거 - 너무 까다로움
    )
    
    if not conditions_met:
        # 상세 로그로 바스켓 상태 출력 (GPT 요구)
        state_detail = f"nf={basket_state['neg_fraction']:.2f},mean={basket_state['mean_score']:.3f},slope={basket_state['slope']:.4f},2tick={basket_state['two_tick']}"
        return {
            "exec_symbol": None,
            "intent": "suppress",
            "route_reason": f"basket_conditions_not_met_{basket_name}({state_detail})"
        }
    
    # ETF 결정
    target_etf = basket_info["etf"]
    
    # 기존 포지션 체크 (GPT 요구: 추가 매수 금지) - 비활성화됨
    # if has_existing_position(target_etf):
    #     return {
    #         "exec_symbol": None,
    #         "intent": "suppress",
    #         "route_reason": f"position_exists_{target_etf}"
    #     }
    
    # ETF 포지션 오픈 가능 체크 (락 및 상충)
    can_open, reason = can_open_etf_position(target_etf)
    if not can_open:
        return {
            "exec_symbol": None,
            "intent": "block",
            "route_reason": f"etf_blocked_{reason}"
        }
    
    # 성공적 라우팅 (상세 로그 포함)
    state_detail = f"nf={basket_state['neg_fraction']:.2f},mean={basket_state['mean_score']:.3f},slope={basket_state['slope']:.4f},2tick={basket_state['two_tick']}"
    return {
        "exec_symbol": target_etf,
        "intent": "enter_inverse",
        "route_reason": f"{basket_name}_cluster->{target_etf}({state_detail})"
    }

def is_actionable_signal(effective_score: float) -> bool:
    """신호가 액션 가능한지 확인"""
    return effective_score >= BUY_THRESHOLD or effective_score <= SELL_THRESHOLD

def claim_idempotency(redis_client, event_id: str, ttl: int = 900) -> bool:
    """중복 이벤트 처리 방지"""
    key = f"seen:{event_id}"
    if redis_client.setnx(key, 1):
        redis_client.expire(key, ttl)
        return True
    return False

def is_in_cooldown(redis_client, symbol: str) -> bool:
    """심볼별 쿨다운 확인"""
    return redis_client.ttl(f"cooldown:{symbol}") > 0

def set_cooldown(redis_client, symbol: str, seconds: int) -> None:
    """심볼별 쿨다운 설정"""
    redis_client.setex(f"cooldown:{symbol}", seconds, 1)

def is_direction_locked(redis_client, symbol: str, wanted_direction: str) -> bool:
    """방향락 확인 (반대 방향 재진입 차단)"""
    current_lock = redis_client.get(f"dirlock:{symbol}")
    if not current_lock:
        return False
    
    current_direction = current_lock.decode()
    # long 원하는데 short 락이 걸려있거나, short 원하는데 long 락이 걸려있으면 차단
    if wanted_direction == "long" and current_direction == "short":
        return True
    if wanted_direction == "short" and current_direction == "long":
        return True
    return False

def set_direction_lock(redis_client, symbol: str, direction: str, seconds: int) -> None:
    """방향락 설정"""
    redis_client.setex(f"dirlock:{symbol}", seconds, direction)

def clear_direction_lock(redis_client, symbol: str) -> None:
    """방향락 해제"""
    redis_client.delete(f"dirlock:{symbol}")

def exceeds_daily_cap(redis_client, symbol: str) -> bool:
    """일일 신호 한도 확인"""
    cap = int(os.getenv("RTH_DAILY_CAP", "100"))
    key = get_daily_key("cap", symbol)
    current_count = int(redis_client.get(key) or 0)
    return current_count >= cap

def count_daily_cap(redis_client, symbol: str) -> bool:
    """일일 신호 카운트 증가 (idempotency 적용)"""
    # GPT 권장: 중복 카운트 방지를 위한 idempotency 키
    slot_id = int(time.time() // 90)  # 90초 슬롯
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    idempotency_key = f"cap:{symbol}:{date_str}:{slot_id}"
    
    # 이미 카운트했으면 스킵
    if not redis_client.setnx(idempotency_key, 1):
        logger.debug(f"일일 캡 중복 카운트 방지: {symbol}")
        return False
    
    redis_client.expire(idempotency_key, 90)  # 90초 TTL
    
    # 실제 카운트 증가
    key = get_daily_key("cap", symbol)
    redis_client.incr(key)
    
    # TTL이 설정되지 않았으면 ET 자정까지 설정
    if redis_client.ttl(key) < 0:
        next_reset = get_next_rth_reset_timestamp()
        redis_client.expireat(key, int(next_reset.timestamp()))
    return True

def get_daily_key(prefix: str, symbol: str) -> str:
    """일일 기준 Redis 키 생성"""
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"{prefix}:{symbol}:{date_str}"

def get_next_rth_reset_timestamp() -> datetime:
    """다음 RTH 리셋 시간 (미 동부시간 자정 기준)"""
    import pytz
    
    # 미 동부시간 자정으로 설정
    et = pytz.timezone("America/New_York")
    now_et = datetime.now(et)
    
    # 다음 자정 계산
    next_midnight = (now_et + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    
    # UTC로 변환하여 반환
    return next_midnight.astimezone(timezone.utc)

def get_open_position(trading_adapter, symbol: str) -> Optional[Dict]:
    """현재 오픈 포지션 확인"""
    try:
        positions = trading_adapter.get_positions()
        for pos in positions:
            if getattr(pos, 'ticker', None) == symbol and float(getattr(pos, 'quantity', 0)) != 0:
                qty = float(pos.quantity)
                return {
                    "symbol": symbol,
                    "qty": qty,
                    "avg_price": float(getattr(pos, 'avg_price', 0) or 0),
                    "side": "long" if qty > 0 else "short",
                    "unrealized_pl": float(getattr(pos, 'unrealized_pnl', 0) or 0),
                    "market_value": float(getattr(pos, 'market_value', 0) or 0),
                    "added_layers": getattr(pos, 'added_layers', 0)
                }
        return None
    except Exception as e:
        logger.error(f"포지션 조회 실패 {symbol}: {e}")
        return None

def get_stop_distance(trading_adapter, symbol: str, fallback_pct: float = None) -> float:
    """스톱 거리 계산 (ATR 기반 또는 퍼센트 폴백)"""
    if fallback_pct is None:
        fallback_pct = STOP_LOSS_PCT
    
    try:
        # 현재 가격 조회
        current_price = trading_adapter.get_current_price(symbol)
        if not current_price or current_price <= 0:
            return 0
        
        # ATR 기반 스톱 거리 계산 시도
        atr = get_atr_from_db(symbol, periods=14)
        if atr and atr > 0:
            # GPT 권장: ATR의 0.7배, 최소 0.4% 보장
            k = 0.7  # 보수적 멀티플라이어
            return max(atr * k, current_price * 0.004)
        
        # 폴백: 고정 퍼센트
        return current_price * fallback_pct
    except Exception as e:
        logger.error(f"스톱 거리 계산 실패 {symbol}: {e}")
        return 0

def get_atr_from_db(symbol: str, periods: int = 14) -> Optional[float]:
    """DB에서 ATR(Average True Range) 계산"""
    try:
        # PostgreSQL 연결
        conn_str = os.getenv("DATABASE_URL")
        if not conn_str:
            return None
        
        conn = psycopg2.connect(conn_str)
        cursor = conn.cursor()
        
        # 최근 N개 캔들에서 TR 계산 후 평균
        query = """
        WITH candles AS (
            SELECT 
                h as high,
                l as low,
                c as close,
                LAG(c) OVER (ORDER BY ts) as prev_close
            FROM bars_30s
            WHERE ticker = %s
            ORDER BY ts DESC
            LIMIT %s
        ),
        true_range AS (
            SELECT 
                GREATEST(
                    high - low,
                    ABS(high - COALESCE(prev_close, close)),
                    ABS(low - COALESCE(prev_close, close))
                ) as tr
            FROM candles
            WHERE prev_close IS NOT NULL
        )
        SELECT AVG(tr) as atr
        FROM true_range
        """
        
        cursor.execute(query, (symbol, periods + 1))
        result = cursor.fetchone()
        
        cursor.close()
        conn.close()
        
        if result and result[0]:
            return float(result[0])
        return None
        
    except Exception as e:
        logger.warning(f"ATR 계산 실패 {symbol}: {e}")
        return None

def calc_entry_quantity(trading_adapter, symbol: str, equity: float, stop_distance: float) -> int:
    """진입 수량 계산 (GPT 권장 리스크 기반 사이징)"""
    try:
        current_price = trading_adapter.get_current_price(symbol)
        if not current_price or current_price <= 0 or stop_distance <= 0:
            return 0
        
        # === 1. 사이징 자본 오버라이드 (GPT 권장) ===
        sizing_mode = os.getenv("SIZING_EQUITY_MODE", "broker")
        if sizing_mode == "override":
            equity_krw = float(os.getenv("SIZING_EQUITY_KRW", "1000000"))
            usd_krw_rate = float(os.getenv("USD_KRW_RATE", "1350"))
            equity_usd = equity_krw / usd_krw_rate
        else:
            # 기존 브로커 계좌 기준 (USD)
            equity_usd = equity
            equity_krw = equity * float(os.getenv("USD_KRW_RATE", "1350"))
        
        # === 2. 고가 종목 하드 필터 (GPT 권장) ===
        max_price_per_share = float(os.getenv("MAX_PRICE_PER_SHARE_USD", "120"))
        if current_price > max_price_per_share and not FRACTIONAL_ENABLED:
            logger.warning(f"🚫 고가종목 스킵: {symbol} ${current_price:.2f} > ${max_price_per_share}")
            return 0  # suppress("price_cap_exceeded")
        
        # 리스크 금액 = 계좌 자산 * 거래당 리스크 비율
        risk_amount_usd = equity_usd * RISK_PER_TRADE
        
        # 포지션 사이즈 = 리스크 금액 / 스톱 거리
        raw_qty = risk_amount_usd / stop_distance
        
        # === 3. 프랙셔널 스위치 명확화 (GPT 권장) ===
        min_notional_usd = float(os.getenv("MIN_ORDER_NOTIONAL_USD", "20"))
        if FRACTIONAL_ENABLED:
            qty = round(raw_qty, 2)  # 소수점 2자리
        else:
            qty = math.floor(raw_qty)
        
        # 최소 주문 금액 체크
        if qty * current_price < min_notional_usd:
            logger.warning(f"🚫 최소 주문금액 미달: {symbol} ${qty * current_price:.2f} < ${min_notional_usd}")
            return 0  # suppress("notional_too_small")
        
        # === 4. 노출 상한 (한화 기준) 세이프티넷 (GPT 권장) ===
        max_notional_krw = float(os.getenv("MAX_NOTIONAL_PER_TRADE_KRW", "600000"))
        notional_krw = qty * current_price * float(os.getenv("USD_KRW_RATE", "1350"))
        if notional_krw > max_notional_krw:
            # 한화 기준 상한에 맞춰 수량 조정
            adjusted_qty = int(max_notional_krw / (current_price * float(os.getenv("USD_KRW_RATE", "1350"))))
            logger.warning(f"⚠️ 한화 노출 상한 조정: {symbol} {qty}주 → {adjusted_qty}주 (₩{notional_krw:,.0f} → ₩{max_notional_krw:,.0f})")
            qty = adjusted_qty
        
        # 최소 수량 보장
        meta = INSTRUMENT_META.get(symbol, {"min_qty": 1})
        min_qty = meta["min_qty"]
        final_qty = max(qty, min_qty) if qty > 0 else 0
        
        # === 5. 사이징 디버그 로그 (GPT 권장) ===
        logger.info(
            f"📊 sizing: equity_krw={equity_krw:,.0f} equity_usd={equity_usd:.0f} "
            f"risk={RISK_PER_TRADE*100}% risk_usd={risk_amount_usd:.2f} "
            f"stop={stop_distance:.2f} last={current_price:.2f} qty={final_qty} "
            f"fractional={'ON' if FRACTIONAL_ENABLED else 'OFF'} "
            f"notional_krw={final_qty * current_price * float(os.getenv('USD_KRW_RATE', '1350')):,.0f} "
            f"decision={'enter' if final_qty > 0 else 'skip'}"
        )
        
        return final_qty
    except Exception as e:
        logger.error(f"진입 수량 계산 실패 {symbol}: {e}")
        return 0

def calc_add_quantity(trading_adapter, symbol: str, position: Dict, equity: float, stop_distance: float) -> int:
    """추가 매수 수량 계산 (GPT 권장 피라미딩)"""
    try:
        # 기본 수량의 50% (GPT 권장: 보수적 피라미딩)
        base_qty = calc_entry_quantity(trading_adapter, symbol, equity, stop_distance)
        add_qty = base_qty * 0.5  # 초기 수량의 50%
        
        # 현재 포지션 크기 고려
        current_qty = abs(position.get("qty", 0))
        current_price = trading_adapter.get_current_price(symbol)
        
        # 추가 후 총 포지션이 계좌의 40%를 초과하지 않도록
        if current_price > 0:
            total_position_value = (current_qty + add_qty) * current_price
            max_allowed_value = equity * 0.4  # 단일 포지션 최대 40%
            
            if total_position_value > max_allowed_value:
                # 조정된 수량 계산
                max_add_value = max_allowed_value - (current_qty * current_price)
                add_qty = max_add_value / current_price
                logger.info(f"⚠️ 피라미딩 수량 조정: {add_qty:.2f}주 (최대 포지션 40% 제한)")
        
        if FRACTIONAL_ENABLED:
            final_qty = round(add_qty, 4)
        else:
            final_qty = max(int(math.floor(add_qty)), 0)
        
        logger.info(f"📊 피라미딩 수량: {symbol} +{final_qty}주 (기존 {current_qty}주)")
        return final_qty
    except Exception as e:
        logger.error(f"추가 수량 계산 실패 {symbol}: {e}")
        return 0

def can_pyramid(trading_adapter, position: Dict, equity: float, stop_distance: float) -> bool:
    """피라미딩 가능 여부 확인 (GPT 권장 로직)"""
    try:
        # 1. 아직 추가매수 안함 (1회만 허용)
        if position.get("added_layers", 0) >= 1:
            logger.debug(f"피라미딩 불가: 이미 {position.get('added_layers', 0)}회 추가매수")
            return False
        
        # 2. 미실현 수익 중 (수익 상태에서만 피라미딩)
        unrealized_pl = position.get("unrealized_pl", 0)
        if unrealized_pl <= 0:
            logger.debug(f"피라미딩 불가: 손실 중 (PL: {unrealized_pl})")
            return False
        
        # 3. 수익률이 최소 스톱 거리의 50% 이상일 때만 (추가 조건)
        avg_price = position.get("avg_price", 0)
        if avg_price > 0:
            profit_pct = unrealized_pl / (avg_price * abs(position.get("qty", 1)))
            min_profit_threshold = STOP_LOSS_PCT * 0.5  # 스톱의 50%
            if profit_pct < min_profit_threshold:
                logger.debug(f"피라미딩 불가: 수익률 부족 ({profit_pct:.2%} < {min_profit_threshold:.2%})")
                return False
        
        # 4. 총 리스크 한도 확인
        current_risk = get_current_total_risk(trading_adapter, equity)
        additional_risk = equity * RISK_PER_TRADE
        max_allowed_risk = equity * MAX_CONCURRENT_RISK
        
        if (current_risk + additional_risk) > max_allowed_risk:
            logger.debug(f"피라미딩 불가: 리스크 한도 초과 ({current_risk + additional_risk:.2f} > {max_allowed_risk:.2f})")
            return False
        
        logger.info(f"✅ 피라미딩 가능: 수익 ${unrealized_pl:.2f}, 리스크 여유 ${max_allowed_risk - current_risk:.2f}")
        return True
    except Exception as e:
        logger.error(f"피라미딩 가능성 확인 실패: {e}")
        return False

def get_current_total_risk(trading_adapter, equity: float) -> float:
    """현재 총 리스크 계산"""
    try:
        total_risk = 0.0
        positions = trading_adapter.get_positions()
        
        for pos in positions:
            if float(getattr(pos, 'quantity', 0)) == 0:
                continue
            
            # 각 포지션의 리스크 = (현재가 - 스톱가) * 수량
            sym = getattr(pos, 'ticker', None)
            if not sym:
                continue
            current_price = trading_adapter.get_current_price(sym)
            stop_distance = get_stop_distance(trading_adapter, sym)
            
            if current_price and stop_distance:
                position_risk = stop_distance * abs(float(getattr(pos, 'quantity', 0)))
                total_risk += position_risk
        
        return total_risk
    except Exception as e:
        logger.error(f"총 리스크 계산 실패: {e}")
        return equity * MAX_CONCURRENT_RISK  # 보수적으로 최대치 반환

def is_eod_window(market_calendar = None) -> bool:
    """장 마감 전 윈도우 확인 (America/New_York 기준)"""
    try:
        now_ny = datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York"))
        eod_minutes_before = EOD_FLATTEN_MINUTES
        close_dt = now_ny.replace(hour=16, minute=0, second=0, microsecond=0)
        start_dt = close_dt - timedelta(minutes=eod_minutes_before)
        in_window = start_dt <= now_ny <= close_dt
        if in_window:
            logger.info(f"🌅 EOD 윈도우 활성: 마감 {eod_minutes_before}분 전")
        return in_window
    except Exception as e:
        logger.error(f"EOD 윈도우 확인 실패: {e}")
        return False

def place_bracket_order(trading_adapter, symbol: str, side: str, quantity: int, stop_distance: float) -> Optional[Dict]:
    """브래킷 주문 (시장가 + OCO) - GPT 권장 구현"""
    try:
        current_price = trading_adapter.get_current_price(symbol)
        if not current_price:
            raise ValueError(f"가격 조회 실패: {symbol}")
        
        # 스톱로스/익절 가격 계산 (GPT 권장: RR 비율 적용)
        if side == "buy":
            stop_price = current_price - stop_distance
            take_profit_price = current_price + (stop_distance * TAKE_PROFIT_RR)
        else:
            stop_price = current_price + stop_distance
            take_profit_price = current_price - (stop_distance * TAKE_PROFIT_RR)
        
        # 스톱/익절 가격 검증
        if stop_price <= 0 or take_profit_price <= 0:
            logger.error(f"❌ 잘못된 OCO 가격: SL=${stop_price:.2f}, TP=${take_profit_price:.2f}")
            raise ValueError("Invalid OCO prices")
        
        # 브래킷 주문 (진입 + 스톱로스 + 익절) 원샷 제출
        try:
            if hasattr(trading_adapter, 'submit_bracket_order'):
                # 알파카 브래킷 주문 사용 (GPT 권장: 원샷 제출)
                main_order, stop_order_id, profit_order_id = trading_adapter.submit_bracket_order(
                    ticker=symbol,
                    side=side,
                    quantity=quantity,
                    stop_loss_price=stop_price,
                    take_profit_price=take_profit_price,
                    signal_id=f"bracket_{symbol}_{int(time.time())}"
                )
                logger.info(f"🎯 브래킷 주문 완료: {symbol} SL=${stop_price:.2f}, TP=${take_profit_price:.2f}, stop_id={stop_order_id}, profit_id={profit_order_id}")
            else:
                # 폴백: 기존 방식 (시장가 + Redis OCO)
                main_order = trading_adapter.submit_market_order(
                    ticker=symbol,
                    side=side,
                    quantity=quantity,
                    signal_id=f"bracket_{symbol}_{int(time.time())}"
                )
                
                # Redis OCO 등록
                redis_client = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379"))
                oco_data = {
                    "symbol": symbol,
                    "quantity": quantity,
                    "stop_loss": stop_price,
                    "take_profit": take_profit_price,
                    "parent_order_id": getattr(main_order, 'id', 'unknown'),
                    "created_at": datetime.now(timezone.utc).isoformat()
                }
                redis_client.hset(
                    f"oco_pending:{symbol}",
                    getattr(main_order, 'id', f"temp_{int(time.time())}"),
                    json.dumps(oco_data)
                )
                redis_client.expire(f"oco_pending:{symbol}", 86400)
                logger.info(f"🎯 폴백 OCO 등록: {symbol} SL=${stop_price:.2f}, TP=${take_profit_price:.2f}")
        except Exception as bracket_e:
            logger.warning(f"브래킷 주문 실패, 폴백 시도: {bracket_e}")
            # 브래킷 실패 시 최소한 시장가 주문만이라도 실행
            main_order = trading_adapter.submit_market_order(
                ticker=symbol,
                side=side,
                quantity=quantity,
                signal_id=f"fallback_{symbol}_{int(time.time())}"
            )
        
        logger.info(
            f"✅ 브래킷 주문 실행: {symbol} {side} {quantity}주 @ ${float(current_price):.2f}\n"
            f"   ├─ 스톱로스: ${float(stop_price):.2f} (-{(stop_distance/current_price)*100:.1f}%)\n"
            f"   └─ 익절목표: ${float(take_profit_price):.2f} (+{(stop_distance*TAKE_PROFIT_RR/current_price)*100:.1f}%)"
        )
        return main_order
        
    except Exception as e:
        logger.error(f"브래킷 주문 실패 {symbol}: {e}")
        raise

def _log_exit_decision(symbol: str, side: str, qty: int, policy: str, reason: str,
                       tif: str = "day", attempt: int = 1, market_state: str = "unknown",
                       order_id: str | None = None, price: float | None = None) -> None:
    parts = [
        f"EXIT_DECISION symbol={symbol}",
        f"side={side}",
        f"qty={qty}",
        f"policy={policy}",
        f"reason={reason}",
        f"tif={tif}",
        f"attempt={attempt}",
        f"market_state={market_state}"
    ]
    if order_id:
        parts.append(f"order_id={order_id}")
    if price is not None:
        try:
            parts.append(f"price={float(price):.2f}")
        except Exception:
            pass
    logger.info(" ".join(parts))

def flatten_all_positions(trading_adapter, reason: str = "eod_flatten") -> int:
    """모든 포지션 강제 청산: 포지션 단위 예외/재시도 + 구조 로그"""
    flattened_count = 0
    positions = []
    try:
        positions = trading_adapter.get_positions() or []
    except Exception as e:
        logger.error(f"포지션 조회 실패: {e}")
        return 0

    failed = []
    for pos in positions:
        try:
            qty_raw = float(getattr(pos, 'quantity', 0))
            if qty_raw == 0:
                continue
            symbol = getattr(pos, 'ticker', '')
            side = "sell" if qty_raw > 0 else "buy"
            quantity = abs(qty_raw if FRACTIONAL_ENABLED else int(qty_raw))

            # 재시도 루프
            last_err = None
            for attempt in range(1, EOD_MAX_RETRIES + 2):  # 최초 1회 + 재시도 N회
                tif_used = "cls"  # 기본 의도는 CLS (submit_eod_exit가 실제 tif를 결정)
                try:
                    # 어댑터가 EOD 전용 출구를 지원하면 우선 사용
                    if hasattr(trading_adapter, "submit_eod_exit"):
                        trade = trading_adapter.submit_eod_exit(symbol, quantity, side)
                    else:
                        tif_used = "day"
                        trade = trading_adapter.submit_market_order(
                            ticker=symbol,
                            side=side,
                            quantity=quantity,
                            signal_id=f"{reason}_{symbol}_{int(time.time())}"
                        )

                    # 기록 및 로깅
                    if trade:
                        # tif 보정 (예약 주문은 meta.tif 보유)
                        try:
                            tif_used = (getattr(trade, 'meta', {}) or {}).get('tif', tif_used)
                        except Exception:
                            pass
                        eod_signal_data = {
                            "symbol": symbol,
                            "score": 0.0,
                            "confidence": 1.0,
                            "regime": "eod",
                            "meta": {"reason": reason, "position_qty": qty_raw, "tif": tif_used, "side": side}
                        }
                        signal_db_id = save_signal_to_db(eod_signal_data, "eod_flatten", f"reason={reason},qty={quantity}")
                        # 예약 주문(CLS/OPG)은 아직 미체결 → 트레이드 기록은 보류
                        if tif_used not in ("cls", "opg"):
                            save_trade_to_db(trade, eod_signal_data, symbol, signal_db_id)

                        order_id = getattr(trade, 'trade_id', None)
                        price = getattr(trade, 'price', None)
                        _log_exit_decision(symbol, side, quantity, policy="EOD", reason=reason, tif=tif_used,
                                           attempt=attempt, market_state="unknown", order_id=order_id, price=price)
                        logger.info(f"EOD 청산: {symbol} {side} {quantity}주 (사유: {reason})")
                        flattened_count += 1
                        break
                except Exception as e:
                    last_err = e
                    _log_exit_decision(symbol, side, quantity, policy="EOD", reason=f"error:{e}", tif=tif_used,
                                       attempt=attempt, market_state="closed")
                    if attempt <= EOD_MAX_RETRIES:
                        try:
                            time.sleep(EOD_RETRY_DELAY_SEC)
                        except Exception:
                            pass
                        continue
                    else:
                        raise
        except Exception as e:
            logger.error(f"포지션 청산 실패: {getattr(pos, 'ticker', '')} - {e}")
            failed.append((getattr(pos, 'ticker', ''), str(e)))
            continue

    # 잔존 포지션 경고
    if failed:
        try:
            slack_bot = trading_components.get("slack_bot")
            if slack_bot:
                msg_lines = ["⚠️ EOD 청산 실패 요약"] + [f"• {sym}: {err}" for sym, err in failed]
                slack_bot.send_message("\n".join(msg_lines))
        except Exception:
            pass

    return flattened_count

def _minutes_to_close(now_utc: datetime | None = None) -> int:
    ny = (now_utc or datetime.now(timezone.utc)).astimezone(ZoneInfo("America/New_York"))
    close_dt = ny.replace(hour=16, minute=0, second=0, microsecond=0)
    delta = (close_dt - ny).total_seconds() / 60.0
    return int(delta)

def _is_rth_now(now_utc: datetime | None = None) -> bool:
    ny = (now_utc or datetime.now(timezone.utc)).astimezone(ZoneInfo("America/New_York"))
    open_dt = ny.replace(hour=9, minute=30, second=0, microsecond=0)
    close_dt = ny.replace(hour=16, minute=0, second=0, microsecond=0)
    return open_dt <= ny <= close_dt

@celery_app.task(bind=True, name="app.jobs.scheduler.queue_preclose_liquidation")
def queue_preclose_liquidation(self):
    """마감 전(ET 15:48) 사전 예약 청산: CLS/OPG 예약 제출"""
    try:
        from app.adapters.trading_adapter import get_trading_adapter
        trading_adapter = get_trading_adapter()
        positions = trading_adapter.get_positions() or []
        scheduled = 0
        for p in positions:
            qty_raw = float(getattr(p, 'quantity', 0))
            quantity = abs(qty_raw if FRACTIONAL_ENABLED else int(qty_raw))
            if quantity <= 0:
                continue
            sym = getattr(p, 'ticker', '')
            try:
                side = "sell" if qty_raw > 0 else "buy"
                if hasattr(trading_adapter, "submit_eod_exit"):
                    trade = trading_adapter.submit_eod_exit(sym, quantity, side)
                else:
                    trade = trading_adapter.submit_market_order(
                        ticker=sym,
                        side=side,
                        quantity=quantity,
                        signal_id=f"preclose_{sym}"
                    )
                scheduled += 1
                tif_used = (getattr(trade, 'meta', {}) or {}).get('tif', 'cls')
                _log_exit_decision(sym, side, quantity, policy="EOD", reason="preclose_queue", tif=tif_used, attempt=1, market_state="RTH",
                                   order_id=getattr(trade, 'trade_id', None), price=getattr(trade, 'price', None))
            except Exception as e:
                logger.warning(f"사전 청산 예약 실패: {sym} - {e}")
        return {"scheduled": scheduled}
    except Exception as e:
        logger.error(f"사전 청산 태스크 실패: {e}")
        return {"error": str(e)}

@celery_app.task(bind=True, name="app.jobs.scheduler.enforce_time_stop")
def enforce_time_stop(self):
    """세션 기준 결정론적 타임 스톱 (기본 ON)"""
    if not ENABLE_TIME_STOP:
        return {"status": "disabled"}
    if not _is_rth_now():
        return {"status": "off_session"}
    try:
        from app.adapters.trading_adapter import get_trading_adapter
        trading_adapter = get_trading_adapter()
        redis_client = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379"))
        positions = trading_adapter.get_positions() or []
        exited = 0
        for p in positions:
            qty_raw = float(getattr(p, 'quantity', 0))
            quantity = abs(qty_raw if FRACTIONAL_ENABLED else int(qty_raw))
            if quantity <= 0:
                continue
            sym = getattr(p, 'ticker', '')
            side = "sell" if qty_raw > 0 else "buy"
            entry_key = f"position_entry_time:{sym}"
            try:
                entry_ts = float(redis_client.get(entry_key) or 0)
            except Exception:
                entry_ts = 0
            if entry_ts <= 0:
                continue
            age_min = int((time.time() - entry_ts) / 60)
            if age_min >= TIME_STOP_MIN:
                # 임박 시 OPG 예약
                minutes_left = _minutes_to_close()
                tif_used = "day"
                try:
                    if minutes_left <= TIME_STOP_LATE_ENTRY_CUTOFF_MIN and hasattr(trading_adapter, "submit_eod_exit"):
                        trade = trading_adapter.submit_eod_exit(sym, quantity, side)
                        try:
                            tif_used = (getattr(trade, 'meta', {}) or {}).get('tif', tif_used)
                        except Exception:
                            pass
                    else:
                        trade = trading_adapter.submit_market_order(ticker=sym, side=side, quantity=quantity, signal_id=f"time_stop_{sym}")
                    exited += 1
                    # DB 기록: 예약 주문은 trade 기록 보류
                    ts_signal = {"symbol": sym, "score": 0.0, "confidence": 1.0, "regime": "time_stop",
                                 "meta": {"reason": "time_stop", "tif": tif_used}}
                    sig_id = save_signal_to_db(ts_signal, "time_stop", f"age_min={age_min}")
                    if tif_used not in ("cls", "opg"):
                        save_trade_to_db(trade, ts_signal, sym, sig_id)
                    _log_exit_decision(sym, side, quantity, policy="TIME_STOP", reason="time_stop", tif=tif_used, attempt=1, market_state="RTH",
                                       order_id=getattr(trade, 'trade_id', None), price=getattr(trade, 'price', None))
                except Exception as e:
                    logger.warning(f"TIME_STOP 청산 실패: {sym} - {e}")
        return {"exited": exited}
    except Exception as e:
        logger.error(f"TIME_STOP 태스크 실패: {e}")
        return {"error": str(e)}

@celery_app.task(bind=True, name="app.jobs.scheduler.enforce_trail_stop")
def enforce_trail_stop(self):
    """결정론적 트레일 스톱 (기본 OFF)"""
    if not ENABLE_TRAIL_STOP:
        return {"status": "disabled"}
    try:
        from app.adapters.trading_adapter import get_trading_adapter
        trading_adapter = get_trading_adapter()
        redis_client = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379"))
        positions = trading_adapter.get_positions() or []
        exited = 0
        failed = []
        for p in positions:
            try:
                qty_raw = float(getattr(p, 'quantity', 0))
                quantity = abs(qty_raw if FRACTIONAL_ENABLED else int(qty_raw))
                if quantity <= 0:
                    continue
                sym = getattr(p, 'ticker', '')
                entry_key = f"position_entry_time:{sym}"
                try:
                    entry_ts = float(redis_client.get(entry_key) or 0)
                except Exception:
                    entry_ts = 0
                if entry_ts <= 0:
                    continue
                age_min = int((time.time() - entry_ts) / 60)
                if age_min < TRAIL_MIN_HOLD_MIN:
                    continue
                price = trading_adapter.get_current_price(sym) or 0.0
                if price <= 0:
                    continue
                peak_key = f"trail:{sym}:peak"
                try:
                    prev_peak = float(redis_client.get(peak_key) or 0)
                except Exception:
                    prev_peak = 0.0
                # 롱 기준 (숏은 추후 확장)
                peak = max(prev_peak, price)
                redis_client.setex(peak_key, 86400, str(peak))
                if peak > 0 and price <= peak * (1 - TRAIL_RET_PCT):
                    side = "sell" if qty_raw > 0 else "buy"
                    tif_used = "day"
                    try:
                        if (not _is_rth_now()) or _minutes_to_close() <= TIME_STOP_LATE_ENTRY_CUTOFF_MIN:
                            if hasattr(trading_adapter, "submit_eod_exit"):
                                trade = trading_adapter.submit_eod_exit(sym, quantity, side)
                                try:
                                    tif_used = (getattr(trade, 'meta', {}) or {}).get('tif', tif_used)
                                except Exception:
                                    pass
                            else:
                                trade = trading_adapter.submit_market_order(ticker=sym, side=side, quantity=quantity, signal_id=f"trail_stop_{sym}")
                        else:
                            trade = trading_adapter.submit_market_order(ticker=sym, side=side, quantity=quantity, signal_id=f"trail_stop_{sym}")
                        exited += 1
                        # 기록
                        tr_signal = {"symbol": sym, "score": 0.0, "confidence": 1.0, "regime": "trail_stop",
                                     "meta": {"reason": "trail_retreat", "tif": tif_used}}
                        sig_id = save_signal_to_db(tr_signal, "trail_stop", f"peak_ret={TRAIL_RET_PCT}")
                        if tif_used not in ("cls", "opg"):
                            save_trade_to_db(trade, tr_signal, sym, sig_id)
                        _log_exit_decision(sym, side, quantity, policy="TRAIL_STOP", reason="trail_retreat", tif=tif_used, attempt=1, market_state=("RTH" if _is_rth_now() else "CLOSED"),
                                           order_id=getattr(trade, 'trade_id', None), price=getattr(trade, 'price', None))
                    except Exception as e:
                        logger.warning(f"TRAIL_STOP 청산 실패: {sym} - {e}")
                        failed.append((sym, str(e)))
                        continue
            except Exception as e:
                logger.warning(f"TRAIL_STOP 처리 실패(심볼 단위): {getattr(p, 'ticker', '')} - {e}")
                failed.append((getattr(p, 'ticker', ''), str(e)))
                continue
        # 요약 경고 (선택)
        if failed:
            try:
                slack_bot = trading_components.get("slack_bot")
                if slack_bot:
                    msg_lines = ["⚠️ TRAIL_STOP 청산 실패 요약"] + [f"• {sym}: {err}" for sym, err in failed]
                    slack_bot.send_message("\n".join(msg_lines))
            except Exception:
                pass
        return {"exited": exited}
    except Exception as e:
        logger.error(f"TRAIL_STOP 태스크 실패: {e}")
        return {"error": str(e)}

@celery_app.task(bind=True, name="app.jobs.scheduler.enforce_regime_flatten_inverse")
def enforce_regime_flatten_inverse(self):
    """역ETF 레짐 플립 평탄화 (옵션)"""
    if not ENABLE_REGIME_FLATTEN_INVERSE:
        return {"status": "disabled"}
    try:
        from app.adapters.trading_adapter import get_trading_adapter
        trading_adapter = get_trading_adapter()
        redis_client = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379"))
        positions = trading_adapter.get_positions() or []
        flattened = 0
        for p in positions:
            sym = getattr(p, 'ticker', '').upper()
            qty_raw = float(getattr(p, 'quantity', 0))
            quantity = abs(qty_raw if FRACTIONAL_ENABLED else int(qty_raw))
            if quantity <= 0 or sym not in INVERSE_TICKERS_SET:
                continue
            # underlying 매핑 (없으면 심볼 자체 기준)
            underlying = INSTRUMENT_META.get(sym, {}).get("underlying", sym)
            # 최신 점수(양수=리스크 온 가정)
            try:
                latest_score = float(redis_client.get(f"latest_score:{underlying}") or 0.0)
            except Exception:
                latest_score = 0.0
            if latest_score >= BUY_THRESHOLD:
                try:
                    side = "sell" if qty_raw > 0 else "buy"
                    trade = trading_adapter.submit_market_order(ticker=sym, side=side, quantity=quantity, signal_id=f"regime_flatten_{sym}")
                    flattened += 1
                    # 기록
                    rg_signal = {"symbol": sym, "score": 0.0, "confidence": 1.0, "regime": "regime_flat",
                                 "meta": {"reason": "regime_flip_inverse_flatten"}}
                    sig_id = save_signal_to_db(rg_signal, "regime_flatten", "inverse_flip")
                    save_trade_to_db(trade, rg_signal, sym, sig_id)
                    _log_exit_decision(sym, side, quantity, policy="REGIME_FLAT", reason="regime_flip_inverse_flatten", tif="day",
                                       attempt=1, market_state="RTH", order_id=getattr(trade, 'trade_id', None), price=getattr(trade, 'price', None))
                except Exception as e:
                    logger.warning(f"레짐 플립 평탄화 실패: {sym} - {e}")
        return {"flattened": flattened}
    except Exception as e:
        logger.error(f"레짐 플립 가드레일 태스크 실패: {e}")
        return {"error": str(e)}

def check_volume_spike(symbol: str, current_volume: float) -> bool:
    """볼륨 스파이크 확인: 현재 볼륨 > 20일 평균 * 1.5배"""
    try:
        # 타임아웃 설정으로 블로킹 방지
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            # 단순화된 쿼리 (최근 5일만 체크)
            query = """
            SELECT AVG(v) as avg_volume
            FROM bars_30s 
            WHERE ticker = %s 
            AND ts >= NOW() - INTERVAL '5 days'
            LIMIT 100
            """
            cursor.execute(query, (symbol,))
            result = cursor.fetchone()
            
            if result and result[0] and current_volume > 0:
                avg_volume = float(result[0])
                volume_threshold = avg_volume * VOLUME_SPIKE_MULTIPLIER
                is_spike = current_volume > volume_threshold
                
                logger.debug(f"볼륨 스파이크 체크 {symbol}: 현재={current_volume:,.0f}, 평균={avg_volume:,.0f}, 스파이크={is_spike}")
                return is_spike
            else:
                # 데이터가 없으면 기본값으로 통과
                return True
                
    except Exception as e:
        logger.error(f"볼륨 스파이크 확인 실패 {symbol}: {e}")
        # 에러 시 기본값으로 통과
        return True

def check_candle_break(symbol: str, current_close: float) -> bool:
    """캔들 브레이크 확인: 현재 종가 > 직전 고점 + epsilon"""
    try:
        # 타임아웃 설정으로 블로킹 방지
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            # 단순화된 쿼리 (최근 5개 캔들만)
            query = """
            SELECT h as high
            FROM bars_30s 
            WHERE ticker = %s 
            AND ts < NOW() - INTERVAL '30 seconds'
            ORDER BY ts DESC 
            LIMIT 5
            """
            cursor.execute(query, (symbol,))
            result = cursor.fetchone()
            
            if result and result[0] and current_close > 0:
                prev_high = float(result[0])
                break_threshold = prev_high * (1 + CANDLE_BREAK_EPSILON)
                is_break = current_close > break_threshold
                
                logger.debug(f"캔들 브레이크 체크 {symbol}: 현재종가={current_close:.2f}, 직전고점={prev_high:.2f}, 브레이크={is_break}")
                return is_break
            else:
                # 데이터가 없으면 기본값으로 통과
                return True
                
    except Exception as e:
        logger.error(f"캔들 브레이크 확인 실패 {symbol}: {e}")
        # 에러 시 기본값으로 통과
        return True

def get_position_entry_time(symbol: str) -> Optional[float]:
    """포지션 진입 시간 조회 (UNIX timestamp)"""
    try:
        # Redis 연결 타임아웃 설정
        r = redis.from_url(os.getenv("REDIS_URL"), socket_timeout=2.0, socket_connect_timeout=2.0)
        entry_key = f"position_entry_time:{symbol}"
        
        # 짧은 타임아웃으로 조회
        entry_time_str = r.get(entry_key)
        
        if entry_time_str:
            return float(entry_time_str)
        else:
            # 포지션 진입 시간이 없으면 현재 시간 반환 (Redis 설정 생략)
            return time.time()
            
    except Exception as e:
        logger.error(f"포지션 진입 시간 조회 실패 {symbol}: {e}")
        # 에러 시 현재 시간 반환
        return time.time()

def close_partial_position(trading_adapter, symbol: str, partial_qty: int, reason: str) -> bool:
    """부분 포지션 청산"""
    try:
        # 현재 포지션 확인
        positions = trading_adapter.get_positions()
        current_pos = None
        
        for pos in positions:
            if pos.ticker == symbol:
                current_pos = pos
                break
        
        if not current_pos or current_pos.quantity <= 0:
            logger.warning(f"청산할 포지션이 없음: {symbol}")
            return False
        
        # 부분 청산량 조정 (보유량을 초과하지 않도록)
        actual_qty = min(partial_qty, abs(int(current_pos.quantity)))
        
        if actual_qty <= 0:
            logger.warning(f"청산할 수량이 없음: {symbol}")
            return False
        
        # 시장가 매도 주문
        side = "sell" if current_pos.quantity > 0 else "buy"
        trade = trading_adapter.submit_market_order(
            ticker=symbol,
            side=side,
            quantity=actual_qty,
            signal_id=f"{reason}_{symbol}_{int(time.time())}"
        )
        
        # GPT 제안: 부분 청산 DB 기록
        if trade:
            partial_signal_data = {
                "symbol": symbol,
                "score": 0.0,  # 부분 청산은 강제 처리
                "confidence": 1.0,
                "regime": "partial_liquidation",
                "meta": {"reason": reason, "partial_qty": actual_qty, "original_qty": current_pos.quantity}
            }
            # 부분 청산: 신호 저장 후 거래 기록
            signal_db_id = save_signal_to_db(partial_signal_data, "partial_liquidate", f"reason={reason},qty={actual_qty}")
            save_trade_to_db(trade, partial_signal_data, symbol, signal_db_id)
        
        logger.info(f"🔄 부분 청산: {symbol} {side} {actual_qty}주 (사유: {reason})")
        return True
        
    except Exception as e:
        logger.error(f"부분 포지션 청산 실패 {symbol}: {e}")
        return False

def check_short_etf_exit_conditions(symbol: str, current_score: float, current_price: float, current_volume: float) -> Optional[str]:
    """숏 ETF 청산 조건 체크"""
    try:
        # 숏 ETF인지 확인
        inverse_etfs = os.getenv("INVERSE_ETFS", "").split(",")
        if symbol not in inverse_etfs:
            return None
        
        # 포지션 진입 시간 확인
        entry_time = get_position_entry_time(symbol)
        if not entry_time:
            return None
        
        hold_time = time.time() - entry_time
        
        # 최소 보유 시간 체크
        if hold_time < MIN_HOLD_SEC:
            logger.debug(f"최소 보유시간 미달: {symbol} {hold_time:.0f}s < {MIN_HOLD_SEC}s")
            return None
        
        # 반대 신호 체크 (롱 신호 = 숏 ETF 청산 신호)
        buy_threshold = float(os.getenv("BUY_THRESHOLD", "0.15"))
        if current_score >= buy_threshold:
            # 볼륨 스파이크 확인
            if not check_volume_spike(symbol, current_volume):
                return "flip_noise_volume"
            
            # 캔들 브레이크 확인
            if not check_candle_break(symbol, current_price):
                return "flip_noise_candle"
            
            return "opposite_signal_confirmed"
        
        return None
        
    except Exception as e:
        logger.error(f"숏 ETF 청산 조건 체크 실패 {symbol}: {e}")
        return None

def log_signal_decision(signal_data: Dict, symbol: str, decision: str, reason: str = None) -> None:
    """신호 처리 결정 로깅"""
    score = signal_data.get("score", 0)
    # score가 문자열로 올 수 있으므로 float로 변환
    try:
        score = float(score) if score else 0
    except (ValueError, TypeError):
        score = 0
    signal_type = signal_data.get("signal_type", "unknown")
    
    if decision == "suppress":
        logger.info(f"🚫 신호 억제: {symbol} {signal_type} (score: {score:.3f}) - {reason}")
    elif decision in ["entry", "add", "exit"]:
        logger.info(f"✅ 신호 실행: {symbol} {signal_type} (score: {score:.3f}) - {decision}")
    else:
        logger.info(f"📝 신호 처리: {symbol} {signal_type} (score: {score:.3f}) - {decision}")

# --- DB 저장 헬퍼 함수 (GPT 제안: 실제 거래 후 로컬 DB 기록) ---

def save_trade_to_db(trade_result, signal_data: Dict, exec_symbol: str, signal_db_id: str = None) -> Optional[str]:
    """거래 결과를 로컬 DB에 저장 - signal_id 연계 개선"""
    if not trade_result:
        return None
        
    dsn = os.getenv("POSTGRES_URL") or os.getenv("DATABASE_URL")
    if not dsn:
        logger.warning("DB DSN이 설정되지 않아 거래 기록을 건너뜀")
        return None
        
    try:
        with psycopg2.connect(dsn) as conn:
            with conn.cursor() as cur:
                # trade_result가 객체인 경우와 dict인 경우 모두 처리
                if hasattr(trade_result, 'side'):
                    # 객체 형태 (알파카 Trade 객체)
                    side = getattr(trade_result, 'side', 'buy')
                    quantity = int(getattr(trade_result, 'quantity', 0))
                    price = float(getattr(trade_result, 'price', 0))
                    trade_id_str = getattr(trade_result, 'trade_id', f"trade_{int(time.time())}")
                else:
                    # dict 형태
                    side = trade_result.get("side", "buy")
                    quantity = int(trade_result.get("quantity", 0))
                    price = float(trade_result.get("price", 0))
                    trade_id_str = trade_result.get("trade_id", f"trade_{int(time.time())}")
                
                # signal_id 결정: 새로 생성된 signal_db_id 우선, 없으면 기존 signal_data에서
                # signals.id는 INTEGER이므로 변환 필요
                final_signal_id = None
                if signal_db_id:
                    try:
                        final_signal_id = int(signal_db_id)
                    except (ValueError, TypeError):
                        final_signal_id = None
                elif signal_data.get("signal_id"):
                    try:
                        final_signal_id = int(signal_data.get("signal_id"))
                    except (ValueError, TypeError):
                        final_signal_id = None
                
                # trades 테이블에 기록 - 실제 스키마 사용
                cur.execute("""
                    INSERT INTO trades (
                        trade_id, ticker, side, quantity, price, signal_id, created_at, status
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (
                    trade_id_str,
                    exec_symbol,
                    side,
                    quantity,
                    price,
                    final_signal_id,  # 유효한 signal_id 사용 또는 NULL
                    datetime.now(),
                    'filled'
                ))
                db_trade_id = cur.fetchone()[0]
                logger.info(f"💾 거래 기록 저장: ID={db_trade_id}, {exec_symbol} {side} {quantity}주 @ ${price:.2f}, signal_id={final_signal_id}")
                return str(db_trade_id)
                
    except Exception as e:
        logger.error(f"거래 DB 저장 실패: {e}")
        return None

def save_signal_to_db(signal_data: Dict, action: str, decision_reason: str) -> Optional[str]:
    """신호를 로컬 DB에 저장"""
    dsn = os.getenv("POSTGRES_URL") or os.getenv("DATABASE_URL")
    if not dsn:
        return None
        
    try:
        with psycopg2.connect(dsn) as conn:
            with conn.cursor() as cur:
                # signals 테이블에 기록
                cur.execute("""
                    INSERT INTO signals (
                        ticker, signal_type, score, confidence, 
                        regime, trigger_reason, created_at, 
                        action_taken, meta
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (
                    signal_data.get("symbol", "UNKNOWN"),
                    "long" if signal_data.get("score", 0) > 0 else "short",
                    float(signal_data.get("score", 0)),
                    float(signal_data.get("confidence", 0.7)),
                    signal_data.get("regime", "trend"),
                    decision_reason,
                    datetime.now(),
                    action,  # "entry", "exit", "suppress"
                    json.dumps(signal_data.get("meta", {}))
                ))
                signal_id = cur.fetchone()[0]
                logger.debug(f"💾 신호 기록 저장: ID={signal_id}, {signal_data.get('symbol')} {action}")
                return str(signal_id)
                
    except Exception as e:
        logger.error(f"신호 DB 저장 실패: {e}")
        return None

@celery_app.task(bind=True, name="app.jobs.scheduler.pipeline_e2e")
def pipeline_e2e(self):
    """포지션 관리 기반 E2E 파이프라인: 신호 소비 → 포지션 상태 기반 거래 실행"""
    try:
        start_time = time.time()
        logger.info("🚀 포지션 관리 E2E 파이프라인 시작")
        
        # 컴포넌트 초기화
        _autoinit_components_if_enabled()
        stream_consumer = trading_components["stream_consumer"]
        slack_bot = trading_components["slack_bot"]
        
        signals_processed = 0
        orders_executed = 0
        signals_suppressed = {"cooldown": 0, "direction_lock": 0, "daily_cap": 0, "below_cutoff": 0, "dup_event": 0, "risk_budget": 0}
        
        # AUTO_MODE 체크
        auto_mode = os.getenv("AUTO_MODE", "0").lower() in ("1", "true", "yes", "on")
        if not auto_mode:
            logger.info("🔄 시뮬레이션 모드 - 실제 거래 건너뜀")
            return {"status": "skipped", "reason": "simulation_mode"}
        
        # 거래 어댑터 초기화
        from app.adapters.trading_adapter import get_trading_adapter
        trading_adapter = get_trading_adapter()
        redis_client = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379"))
        
        # 계좌 정보 조회
        account_info = trading_adapter.get_portfolio_summary()
        equity = float(account_info.get('equity', 100000))
        logger.info(f"💰 현재 계좌 자산: ${equity:,.2f}")
        
        # EOD 윈도우 체크 - 강제 청산
        if is_eod_window():
            flattened = flatten_all_positions(trading_adapter, "eod_flatten")
            logger.info(f"🌅 EOD 윈도우: {flattened}개 포지션 강제 청산")
            return {
                "status": "eod_flatten",
                "positions_flattened": flattened,
                "execution_time": time.time() - start_time
            }
        
        # 🎯 숏 ETF 청산 조건 체크 - 비활성화 (2025-08-20)
        # 정규장 직전 전량 청산만 유지하기로 결정
        # liquidation_count = 0
        # try:
        #     # 현재 포지션을 한 번만 조회하여 성능 최적화
        #     positions = trading_adapter.get_positions() if trading_adapter else []
        #     inverse_etfs = set(os.getenv("INVERSE_ETFS", "SOXS,SQQQ,SPXS,TZA,SDOW,TECS").split(","))
        #     
        #     for pos in positions:
        #         if (hasattr(pos, 'ticker') and pos.ticker in inverse_etfs and 
        #             hasattr(pos, 'quantity') and float(pos.quantity) > 0):
        #             
        #             # 포지션 기본 정보
        #             symbol = pos.ticker
        #             qty = float(pos.quantity)
        #             
        #             try:
        #                 # 최신 가격 및 점수 확인 (캐시된 값 활용)
        #                 current_price = trading_adapter.get_current_price(symbol)
        #                 if current_price <= 0:
        #                     continue
        #                 
        #                 # 간단한 청산 조건: BUY_THRESHOLD 이상의 점수 (롱 전환)
        #                 # 복잡한 볼륨/캔들 체크는 성능상 생략
        #                 latest_signal = redis_client.get(f"latest_score:{symbol}")
        #                 if latest_signal:
        #                     try:
        #                         current_score = float(latest_signal)
        #                         if current_score >= BUY_THRESHOLD:
        #                             # 부분 청산 (50%)
        #                             partial_qty = max(1, int(qty * 0.5))
        #                             
        #                             # 청산 주문 실행
        #                             trade = trading_adapter.submit_market_order(
        #                                 ticker=symbol,
        #                                 side="sell",
        #                                 quantity=partial_qty,
        #                                 signal_id=f"liquidation_{symbol}_{int(time.time())}"
        #                             )
        #                             
        #                             if trade:
        #                                 liquidation_count += 1
        #                                 logger.info(f"🔄 숏 ETF 부분 청산: {symbol} {partial_qty}주 @ ${current_price:.2f} (스코어: {current_score:.3f})")
        #                                 
        #                                 # Slack 알림
        #                                 if slack_bot:
        #                                     slack_message = f"🔄 *롱 전환 청산*\n• {symbol} {partial_qty}주 매도 @ ${current_price:.2f}\n• 전환 스코어: {current_score:.3f}"
        #                                     slack_bot.send_message(slack_message)
        #                     except ValueError:
        #                         continue
        #             except Exception as e:
        #                 logger.debug(f"숏 ETF 청산 체크 실패 {symbol}: {e}")
        #                 continue
        #     
        #     if liquidation_count > 0:
        #         logger.info(f"✅ 숏 ETF 청산 완료: {liquidation_count}개 포지션 처리")
        # 
        # except Exception as e:
        #     logger.error(f"숏 ETF 청산 로직 오류: {e}")
        #     # 오류가 발생해도 전체 파이프라인은 계속 진행
        
        # Redis 스트림에서 신호 소비
        redis_streams = stream_consumer.redis_streams
        raw_signals = redis_streams.consume_stream("signals.raw", count=50, block_ms=0, last_id="0")
        logger.info(f"📊 Redis에서 {len(raw_signals)}개 신호 수신")
        
        # 리스크 예산 계산
        current_total_risk = get_current_total_risk(trading_adapter, equity)
        risk_budget_left = (equity * MAX_CONCURRENT_RISK) - current_total_risk
        logger.info(f"🛡️ 총 리스크: ${current_total_risk:.2f}, 잔여 예산: ${risk_budget_left:.2f}")
        
        for signal_event in raw_signals:
            try:
                signal_data = signal_event.data
                event_id = signal_event.message_id
                
                # 신호 기본 정보 추출
                symbol = signal_data.get("ticker")
                def parse_numeric_value(value):
                    """numpy 및 기타 타입을 float로 안전하게 변환"""
                    try:
                        if hasattr(value, 'item'):  # numpy scalar
                            return float(value.item())
                        
                        value_str = str(value)
                        # numpy float64(...) 형태 처리
                        if value_str.startswith('np.float64(') and value_str.endswith(')'):
                            value_str = value_str[11:-1]
                        elif value_str.startswith('np.float32(') and value_str.endswith(')'):
                            value_str = value_str[11:-1]
                        
                        return float(value_str)
                    except Exception:
                        return 0.0
                
                base_score = parse_numeric_value(signal_data.get("score", 0))
                signal_type = signal_data.get("signal_type", "unknown")
                
                if not symbol or symbol not in INSTRUMENT_META:
                    log_signal_decision(signal_data, symbol or "unknown", "suppress", "unknown_symbol")
                    continue
                
                # 🔄 심볼 라우팅 (바스켓 기반)
                route_result = route_signal_symbol(symbol, base_score)
                exec_symbol = route_result["exec_symbol"]
                route_reason = route_result["route_reason"]
                route_intent = route_result.get("intent", "")
                
                # 라우팅 결과에 따른 처리
                if exec_symbol is None:
                    # skip, suppress, block 등의 경우
                    log_signal_decision(signal_data, symbol, route_intent, route_reason)
                    if "basket_conditions_not_met" in route_reason:
                        signals_suppressed["below_cutoff"] += 1
                    elif "etf_locked" in route_reason:
                        signals_suppressed["cooldown"] += 1
                    elif "conflict" in route_reason:
                        signals_suppressed["direction_lock"] += 1
                    continue
                
                # 실행 심볼의 메타데이터 확인
                if exec_symbol not in INSTRUMENT_META:
                    log_signal_decision(signal_data, symbol, "suppress", f"routed_symbol_unknown:{exec_symbol}")
                    continue
                
                # 라우팅된 심볼로 effective_score 계산
                exec_meta = INSTRUMENT_META[exec_symbol]
                effective_score = exec_meta["exposure_sign"] * base_score
                
                # 액션 가능성 확인
                if not is_actionable_signal(effective_score):
                    log_signal_decision(signal_data, symbol, "suppress", f"below_cutoff:routed_to_{exec_symbol}")
                    signals_suppressed["below_cutoff"] += 1
                    continue
                
                logger.info(f"🔄 라우팅: {route_reason}, 스코어: {base_score:.3f} → {effective_score:.3f}")
                
                # 중복 이벤트 차단
                if not claim_idempotency(redis_client, event_id, ttl=900):
                    log_signal_decision(signal_data, symbol, "suppress", "dup_event")
                    signals_suppressed["dup_event"] += 1
                    continue
                
                # 쿨다운 확인 (실행 심볼 기준)
                if is_in_cooldown(redis_client, exec_symbol):
                    log_signal_decision(signal_data, symbol, "suppress", f"cooldown:{exec_symbol}")
                    signals_suppressed["cooldown"] += 1
                    continue
                
                # 일일 신호 한도 확인 (실행 심볼 기준)
                if exceeds_daily_cap(redis_client, exec_symbol):
                    log_signal_decision(signal_data, symbol, "suppress", f"daily_cap:{exec_symbol}")
                    signals_suppressed["daily_cap"] += 1
                    continue
                
                # 현재 포지션 확인 (실행 심볼 기준)
                current_position = get_open_position(trading_adapter, exec_symbol)
                
                # 거래 방향 결정
                if effective_score >= BUY_THRESHOLD:
                    wanted_direction = "long"
                    action = "buy"
                elif effective_score <= SELL_THRESHOLD:
                    wanted_direction = "exit"
                    action = "sell"
                else:
                    log_signal_decision(signal_data, symbol, "suppress", "neutral_zone")
                    continue
                
                # 방향락 확인 (실행 심볼 기준)
                if is_direction_locked(redis_client, exec_symbol, wanted_direction):
                    log_signal_decision(signal_data, symbol, "suppress", f"direction_lock:{exec_symbol}")
                    signals_suppressed["direction_lock"] += 1
                    continue
                
                # 스톱 거리 계산 (실행 심볼 기준)
                stop_distance = get_stop_distance(trading_adapter, exec_symbol)
                if stop_distance <= 0:
                    log_signal_decision(signal_data, symbol, "suppress", f"invalid_stop_distance:{exec_symbol}")
                    continue
                
                # 거래 실행 로직
                if action == "buy":
                    if current_position:
                        # 추가 매수 (피라미딩) 검토
                        if can_pyramid(trading_adapter, current_position, equity, stop_distance):
                            quantity = calc_add_quantity(trading_adapter, exec_symbol, current_position, equity, stop_distance)
                            if quantity > 0:
                                trade = place_bracket_order(trading_adapter, exec_symbol, "buy", quantity, stop_distance)
                                set_cooldown(redis_client, exec_symbol, COOLDOWN_SECONDS)
                                set_direction_lock(redis_client, exec_symbol, "long", DIRECTION_LOCK_SECONDS)
                                count_daily_cap(redis_client, exec_symbol)
                                orders_executed += 1
                                log_signal_decision(signal_data, symbol, "add", f"exec_symbol={exec_symbol},qty={quantity}")
                                
                                # GPT 제안: DB 저장 로직 추가 - 신호 먼저 저장 후 거래에 연결
                                if trade:
                                    signal_db_id = save_signal_to_db(signal_data, "add", f"exec_symbol={exec_symbol},qty={quantity}")
                                    save_trade_to_db(trade, signal_data, exec_symbol, signal_db_id)
                                
                                # Slack 알림
                                if slack_bot:
                                    slack_message = f"📈 *추가 매수*\n• {exec_symbol} +{quantity}주 @ ${float(getattr(trade, 'price', 0)):.2f}\n• 원신호: {symbol}({base_score:.3f})\n• 라우팅: {route_reason}\n• 기존포지션: {current_position['qty']}주"
                                    slack_bot.send_message(slack_message)
                            else:
                                log_signal_decision(signal_data, symbol, "suppress", f"qty_zero_add:{exec_symbol}")
                        else:
                            log_signal_decision(signal_data, symbol, "suppress", f"already_long_no_pyramid:{exec_symbol}")
                    else:
                        # 신규 진입
                        if risk_budget_left <= 0:
                            log_signal_decision(signal_data, symbol, "suppress", "risk_budget_exhausted")
                            signals_suppressed["risk_budget"] += 1
                            continue
                        
                        quantity = calc_entry_quantity(trading_adapter, exec_symbol, equity, stop_distance)
                        if quantity > 0:
                            trade = place_bracket_order(trading_adapter, exec_symbol, "buy", quantity, stop_distance)
                            set_cooldown(redis_client, exec_symbol, COOLDOWN_SECONDS)
                            set_direction_lock(redis_client, exec_symbol, "long", DIRECTION_LOCK_SECONDS)
                            count_daily_cap(redis_client, exec_symbol)
                            risk_budget_left -= (equity * RISK_PER_TRADE)
                            
                            # 포지션 진입 시간 기록 (숏 ETF 청산 로직용)
                            entry_key = f"position_entry_time:{exec_symbol}"
                            redis_client.set(entry_key, time.time(), ex=86400)  # 24시간 TTL
                            
                            orders_executed += 1
                            log_signal_decision(signal_data, symbol, "entry", f"exec_symbol={exec_symbol},qty={quantity}")
                            
                            # GPT 제안: DB 저장 로직 추가 - 신호 먼저 저장 후 거래에 연결
                            if trade:
                                signal_db_id = save_signal_to_db(signal_data, "entry", f"exec_symbol={exec_symbol},qty={quantity}")
                                save_trade_to_db(trade, signal_data, exec_symbol, signal_db_id)
                            
                            # Slack 알림
                            if slack_bot:
                                slack_message = f"🚀 *신규 진입*\n• {exec_symbol} {quantity}주 @ ${float(getattr(trade, 'price', 0)):.2f}\n• 원신호: {symbol}({base_score:.3f})\n• 라우팅: {route_reason}\n• 스톱거리: ${float(stop_distance):.2f}"
                                slack_bot.send_message(slack_message)
                        else:
                            log_signal_decision(signal_data, symbol, "suppress", f"qty_zero_entry:{exec_symbol}")
                
                elif action == "sell":
                    if current_position:
                        # 포지션 청산 (실행 심볼 기준)
                        quantity = abs(current_position["qty"])
                        trade = trading_adapter.submit_market_order(
                            ticker=exec_symbol,
                            side="sell",
                            quantity=quantity,
                            signal_id=f"exit_{exec_symbol}_{int(time.time())}"
                        )
                        clear_direction_lock(redis_client, exec_symbol)
                        set_cooldown(redis_client, exec_symbol, COOLDOWN_SECONDS // 2)  # 청산 후 짧은 쿨다운
                        count_daily_cap(redis_client, exec_symbol)
                        orders_executed += 1
                        log_signal_decision(signal_data, symbol, "exit", f"exec_symbol={exec_symbol},qty={quantity}")
                        
                        # GPT 제안: DB 저장 로직 추가 - 신호 먼저 저장 후 거래에 연결
                        if trade:
                            signal_db_id = save_signal_to_db(signal_data, "exit", f"exec_symbol={exec_symbol},qty={quantity}")
                            save_trade_to_db(trade, signal_data, exec_symbol, signal_db_id)
                        
                        # Slack 알림
                        if slack_bot:
                            pnl = current_position["unrealized_pl"]
                            pnl_emoji = "📈" if pnl >= 0 else "📉"
                            slack_message = f"{pnl_emoji} *포지션 청산*\n• {exec_symbol} -{quantity}주 @ ${float(getattr(trade, 'price', 0)):.2f}\n• 원신호: {symbol}({base_score:.3f})\n• 라우팅: {route_reason}\n• 손익: ${float(pnl):.2f}"
                            slack_bot.send_message(slack_message)
                    else:
                        log_signal_decision(signal_data, symbol, "suppress", f"no_position_to_exit:{exec_symbol}")
                
                # 메시지 ACK
                stream_consumer.acknowledge("signals.raw", signal_event.message_id)
                signals_processed += 1
                
            except Exception as sig_e:
                logger.error(f"신호 처리 실패 {signal_event.message_id}: {sig_e}")
                import traceback
                traceback.print_exc()
                continue
        
        execution_time = time.time() - start_time
        
        # 성능 통계 로깅
        total_signals = signals_processed + sum(signals_suppressed.values())
        logger.info(f"🎯 파이프라인 완료: {execution_time:.2f}초")
        logger.info(f"📊 신호 통계: 총 {total_signals}개, 처리 {signals_processed}개, 주문 {orders_executed}개")
        logger.info(f"🚫 억제 통계: {signals_suppressed}")
        
        return {
            "status": "success",
            "signals_processed": signals_processed,
            "orders_executed": orders_executed,
            "signals_suppressed": signals_suppressed,
            "risk_budget_used": current_total_risk,
            "risk_budget_left": risk_budget_left,
            "execution_time": execution_time,
            "timestamp": datetime.now().isoformat()
        }
        
    except Exception as e:
        logger.error(f"E2E 파이프라인 실패: {e}")
        import traceback
        traceback.print_exc()
        return {
            "status": "error",
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }

def get_mock_candles(ticker: str) -> List:
    """모의 캔들 데이터"""
    # 실제로는 quotes_ingestor에서 가져옴
    return []

def get_mock_indicators(ticker: str) -> Dict:
    """모의 기술적 지표"""
    # 실제로는 quotes_ingestor에서 가져옴
    return {
        "adx": 25.0,
        "ema_20": 150.0,
        "ema_50": 148.0,
        "rsi": 65.0,
        "volume_spike": 0.3,
        "price_change_5m": 0.02,
        "realized_volatility": 0.03,
        "current_price": 150.0,
        "vwap": 149.5,
        "vwap_deviation": 0.003,
        "macd": 0.5,
        "macd_signal": 0.3,
        "bb_position": 0.6
    }

def get_mock_tech_score(ticker: str):
    """모의 기술적 점수"""
    # 실제로는 tech_score_engine에서 계산
    from app.engine.techscore import TechScoreResult
    return TechScoreResult(
        score=0.7,
        components={
            "ema": 0.8,
            "macd": 0.7,
            "rsi": 0.6,
            "vwap": 0.7,
        },
        timestamp=datetime.now()
    )

def format_slack_message(signal) -> Dict:
    """슬랙 메시지 포맷 (가격/버튼/상태정보 포함)"""
    import os
    import json
    
    # 기본 메시지 텍스트
    regime_display = signal.regime.upper().replace("_", "")
    score_sign = "+" if signal.score >= 0 else ""
    action = "long" if signal.signal_type.value == "long" else "short"
    action_ko = "롱" if action == "long" else "숏"
    
    # 메인 메시지
    main_text = (
        f"{signal.ticker} | 레짐 {regime_display}({signal.confidence:.2f}) | "
        f"점수 {score_sign}{signal.score:.2f} {action_ko}"
    )
    
    # 디테일 라인
    detail_text = (
        f"제안: 진입 {signal.entry_price:.2f} / "
        f"손절 {signal.stop_loss:.2f} / 익절 {signal.take_profit:.2f}\n"
        f"이유: {signal.trigger} (<= {signal.horizon_minutes}m)"
    )
    
    # 버튼 생성 여부 확인 (반자동 모드만)
    show_buttons = os.getenv("SEMI_AUTO_BUTTONS", "0").lower() in ("1", "true", "yes", "on")
    
    # 블록 구성
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{main_text}*"}
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": detail_text}
        }
    ]
    
    # 버튼 추가 (반자동 모드일 때)
    if show_buttons:
        button_text = "매수" if action == "long" else "매도"
        order_payload = json.dumps({
            "ticker": signal.ticker,
            "side": "buy" if action == "long" else "sell",
            "entry": signal.entry_price,
            "sl": signal.stop_loss,
            "tp": signal.take_profit,
            "qty": int(os.getenv("DEFAULT_QTY", "1"))
        })
        
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": f"✅ {button_text}", "emoji": True},
                    "style": "primary",
                    "value": order_payload,
                    "action_id": "approve_trade"
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "❌ 패스", "emoji": True},
                    "style": "danger",
                    "value": f"reject_{signal.ticker}_{signal.signal_type.value}_{signal.timestamp.timestamp()}",
                    "action_id": "reject_trade"
                }
            ]
        })
    
    # 첫 번째 채널 우선, 두 번째는 폴백 (channel_not_found 오류 해결)
    channel = os.getenv("SLACK_CHANNEL_ID") or "C099CQP8CJ3"
    
    return {
        "channel": channel,
        "text": main_text,
        "blocks": blocks
    }

def format_enhanced_slack_message(signal, llm_insight) -> Dict:
    """LLM 분석이 포함된 향상된 슬랙 메시지 포맷 (Phase 1.5)"""
    import os
    import json
    
    # 기본 정보
    signal.regime.upper().replace("_", "")
    action_ko = "롱" if signal.signal_type.value == "long" else "숏"
    
    # 신뢰도 이모지
    confidence_emoji = {5: "🎯", 4: "👍", 3: "🤔", 2: "⚠️", 1: "😅"}
    confidence_level = min(5, max(1, round(signal.confidence * 5)))
    
    # LLM 향상된 메인 메시지
    main_text = f"""
💭 **{signal.ticker} 새로운 기회 발견!**

{confidence_emoji[confidence_level]} **AI 판단**: {action_ko} 추천 ({signal.score:+.2f}점)
🎯 **AI 분석**: {llm_insight.summary}"""

    # 상세 정보 (더 친근하게)
    detail_text = f"""
💡 **트레이딩 제안**:
• 진입가: ${signal.entry_price:.2f}
• 손절선: ${signal.stop_loss:.2f} (리스크 관리)
• 목표가: ${signal.take_profit:.2f} (수익 기대)

🧠 **AI가 본 이유**: {llm_insight.trigger}
⏰ **예상 지속시간**: {signal.horizon_minutes}분

어떻게 하시겠어요?"""

    # 블록 구성
    blocks = [
        {
            "type": "section", 
            "text": {"type": "mrkdwn", "text": main_text}
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": detail_text}
        }
    ]
    
    # 버튼 추가 (반자동 모드만)
    show_buttons = os.getenv("SEMI_AUTO_BUTTONS", "0").lower() in ("1", "true", "yes", "on")
    
    if show_buttons:
        button_text = "매수" if signal.signal_type.value == "long" else "매도"
        order_payload = json.dumps({
            "ticker": signal.ticker,
            "action": signal.signal_type.value,
            "entry_price": signal.entry_price,
            "stop_loss": signal.stop_loss,
            "take_profit": signal.take_profit,
            "signal_score": signal.score,
            "llm_analysis": {
                "sentiment": llm_insight.sentiment,
                "trigger": llm_insight.trigger,
                "summary": llm_insight.summary
            }
        })
        
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": f"✅ {button_text}"},
                    "style": "primary",
                    "value": order_payload,
                    "action_id": "execute_trade"
                },
                {
                    "type": "button", 
                    "text": {"type": "plain_text", "text": "❌ 건너뛰기"},
                    "value": "skip",
                    "action_id": "skip_trade"
                }
            ]
        })
    
    # 채널 설정
    channel = os.getenv("SLACK_CHANNEL_ID", "#trading-signals")
    
    return {
        "channel": channel,
        "text": f"🤖 AI 추천: {signal.ticker} {action_ko} {signal.score:+.2f}점",
        "blocks": blocks
    }

@celery_app.task(bind=True, name="app.jobs.scheduler.generate_signals")
def generate_signals(self):
    """시그널 생성 작업"""
    try:
        start_time = time.time()
        logger.info("시그널 생성 시작")
        
        # 필수 최소 컴포넌트 확인: 스캘프 경로만이라도 돌릴 수 있게 최소 deps만 강제
        # 1) quotes_ingestor 필수. 없으면 현 자리에서 생성 시도
        if not trading_components.get("quotes_ingestor"):
            logger.warning("필수 컴포넌트 미준비: ['quotes_ingestor'] → 로컬 생성 시도")
            try:
                from app.io.quotes_delayed import DelayedQuotesIngestor
                trading_components["quotes_ingestor"] = DelayedQuotesIngestor()
                try:
                    trading_components["quotes_ingestor"].warmup_backfill()  # type: ignore
                except Exception:
                    pass
            except Exception as e:
                logger.warning(f"quotes_ingestor 생성 실패: {e}")
        if not trading_components.get("quotes_ingestor"):
            return {"status": "skipped", "reason": "quotes_ingestor_not_ready"}
        # 2) signal_mixer 없으면 현 자리에서 기본값으로 생성 (스캘프 경로용)
        if not trading_components.get("signal_mixer"):
            try:
                from app.engine.mixer import SignalMixer
                thr = settings.MIXER_THRESHOLD
                trading_components["signal_mixer"] = SignalMixer(buy_threshold=thr, sell_threshold=-thr)
                logger.warning("signal_mixer 로컬 생성")
            except Exception as e:
                logger.warning(f"signal_mixer 생성 실패: {e}")
        # 3) redis_streams 없으면 현 자리에서 생성 (발행용)
        if not trading_components.get("redis_streams"):
            try:
                from app.io.streams import RedisStreams
                rurl = os.getenv("REDIS_URL", "redis://redis:6379/0")
                host, port, db = _parse_redis_url(rurl)
                trading_components["redis_streams"] = RedisStreams(host=host, port=port, db=db)
            except Exception as e:
                logger.warning(f"redis_streams 생성 실패: {e}")
        
        quotes_ingestor = trading_components["quotes_ingestor"]
        trading_components["edgar_scanner"]
        regime_detector = trading_components["regime_detector"]
        tech_score_engine = trading_components["tech_score_engine"]
        llm_engine = trading_components["llm_engine"]
        signal_mixer = trading_components["signal_mixer"]
        redis_streams = trading_components["redis_streams"]
        slack_bot = trading_components.get("slack_bot")
        
        # 🔍 DEBUG: Slack bot 상태 확인
        logger.info(f"🔍 [DEBUG] slack_bot 존재 여부: {slack_bot is not None}")
        if slack_bot:
            logger.info(f"🔍 [DEBUG] slack_bot 타입: {type(slack_bot)}")
            logger.info(f"🔍 [DEBUG] slack_bot 채널: {getattr(slack_bot, 'default_channel', 'N/A')}")
        else:
            logger.warning(f"🔍 [DEBUG] slack_bot이 None임! trading_components 키: {list(trading_components.keys())}")
        
        signals_generated = 0
        
        # 각 종목별로 시그널 생성
        # 유니버스 동적 적용 (Redis union: core + external + watchlist)
        dynamic_universe = None
        try:
            rurl = os.getenv("REDIS_URL")
            if rurl:
                r = redis.from_url(rurl)
                external = r.smembers("universe:external") or []
                watch = r.smembers("universe:watchlist") or []
                core = [t.strip().upper() for t in (os.getenv("TICKERS", "").split(",")) if t.strip()]
                ext = [x.decode() if isinstance(x, (bytes, bytearray)) else x for x in external]
                wch = [x.decode() if isinstance(x, (bytes, bytearray)) else x for x in watch]
                merged = []
                seen = set()
                max_n = int(os.getenv("UNIVERSE_MAX", "100"))
                for arr in [core, ext, wch]:
                    for s in arr:
                        if s and s not in seen:
                            merged.append(s)
                            seen.add(s)
                        if len(merged) >= max_n:
                            break
                    if len(merged) >= max_n:
                        break
                dynamic_universe = merged
                # 인제스터에 반영 및 워밍업
                try:
                    if hasattr(quotes_ingestor, "update_universe_tickers"):
                        quotes_ingestor.update_universe_tickers(dynamic_universe)
                except Exception:
                    pass
        except Exception:
            dynamic_universe = None

        # Tier 기반 종목 처리 (Universe Expansion)
        universe_tiers = get_universe_with_tiers()
        logger.info(f"🎯 Tier 유니버스: A={len(universe_tiers['tier_a'])}, B={len(universe_tiers['tier_b'])}, 벤치={len(universe_tiers['bench'])}")
        
        # Tier별 스케줄링 적용
        current_time = datetime.now()
        processing_tickers = []
        
        # Tier A 종목 체크 (30초마다)
        for ticker in universe_tiers['tier_a']:
            should_process, reason = should_process_ticker_now(ticker, current_time)
            if should_process:
                processing_tickers.append((ticker, TokenTier.TIER_A, reason))
        
        # Tier B 종목 체크 (60초마다) 
        for ticker in universe_tiers['tier_b']:
            should_process, reason = should_process_ticker_now(ticker, current_time)
            if should_process:
                processing_tickers.append((ticker, TokenTier.TIER_B, reason))
        
        # 벤치 종목은 현재 이벤트 기반만 (추후 확장)
        
        logger.info(f"🎯 처리 대상: {len(processing_tickers)}개 종목 {[f'{t}({tier.value})' for t, tier, _ in processing_tickers]}")
        
        # Fallback: Tier 시스템 비활성화시 기존 방식 사용
        if not processing_tickers:
            logger.info("🎯 Tier 처리 대상 없음, 기존 방식으로 Fallback")
            tickers_iter = dynamic_universe or list(quotes_ingestor.tickers)
            processing_tickers = [(ticker, None, "fallback") for ticker in tickers_iter]
        
        for ticker, tier, schedule_reason in processing_tickers:
            try:
                # API 토큰 체크 및 소비 (Tier 시스템)
                if tier is not None:  # Tier 시스템 활성화된 경우
                    can_consume, token_reason = can_consume_api_token_for_ticker(ticker)
                    if not can_consume:
                        logger.info(f"🚫 토큰 부족으로 스킵: {ticker} ({tier.value}) - {token_reason}")
                        continue
                    
                    # 토큰 실제 소비
                    consumed, consume_reason = consume_api_token_for_ticker(ticker)
                    if not consumed:
                        logger.warning(f"🚫 토큰 소비 실패: {ticker} ({tier.value}) - {consume_reason}")
                        continue
                    
                    logger.debug(f"✅ 토큰 소비: {ticker} ({tier.value}) - {consume_reason}")
                
                # 1. 시세 데이터 가져오기
                candles = quotes_ingestor.get_latest_candles(ticker, 50)
                if len(candles) < 20:
                    continue
                
                # 2. 기술적 지표 계산
                indicators = quotes_ingestor.get_technical_indicators(ticker)
                if not indicators:
                    continue
                
                # 2.5 스캘프 모드: 마지막 한 틱이 크게 튀면 즉시 신호 생성 (테스트/알림용)
                try:
                    scalp_enabled = (os.getenv("SCALP_TICK_SPIKE", "false").lower() in ("1", "true", "yes", "on"))
                    if scalp_enabled and len(candles) >= 2:
                        # 30s/15s 분할 시 마지막 두 바가 같은 1분에 속함 → 분당 간격으로 비교
                        try:
                            bars_per_min = max(1, 60 // max(getattr(quotes_ingestor, "bar_sec", 60), 1))
                        except Exception:
                            bars_per_min = 1
                        last = candles[-1]
                        prev_idx = -1 - bars_per_min if len(candles) > bars_per_min else -2
                        prev = candles[prev_idx]
                        last_abs_ret = abs((last.c - prev.c) / max(prev.c, 1e-9))
                        spike_thr = float(os.getenv("SCALP_MIN_RET", "0.003"))  # 기본 0.3%
                        # 야후 1분봉 분할 시 마지막 두 바가 동일 값인 경우가 많아 고저폭 기준도 허용
                        last_range = (last.h - last.l) / max(last.c, 1e-9)
                        range_thr = float(os.getenv("SCALP_MIN_RANGE", "0.003"))  # 기본 0.3%
                        trig_reason = None
                        if last_abs_ret >= spike_thr:
                            trig_reason = f"ret>={spike_thr:.3%}"
                        elif last_range >= range_thr:
                            trig_reason = f"range>={range_thr:.3%}"
                        if trig_reason:
                            from app.engine.regime import RegimeType, RegimeResult
                            from app.engine.techscore import TechScoreResult
                            direction = 1.0 if (last.c - prev.c) >= 0 else -1.0
                            fake_regime = RegimeResult(
                                regime=RegimeType.VOL_SPIKE,
                                confidence=min(1.0, last_abs_ret * 20.0),
                                features={"last_abs_ret": last_abs_ret},
                                timestamp=datetime.now()
                            )
                            fake_tech = TechScoreResult(
                                score=direction,  # -1 ~ +1로 직접 스케일
                                components={"ema": 0.8 if direction > 0 else 0.2,
                                            "macd": 0.8 if direction > 0 else 0.2,
                                            "rsi": 0.8 if direction > 0 else 0.2,
                                            "vwap": 0.8 if direction > 0 else 0.2},
                                timestamp=datetime.now()
                            )
                            quick_signal = signal_mixer.mix_signals(
                                ticker=ticker,
                                regime_result=fake_regime,
                                tech_score=fake_tech,
                                llm_insight=None,
                                edgar_filing=None,
                                current_price=last.c
                            )
                            if quick_signal:
                                quick_signal.trigger = "tick_spike"
                                quick_signal.summary = f"{trig_reason}; abs_ret={last_abs_ret:.3%}; range={last_range:.3%}"
                                # 컷/세션 억제 동일 적용 (로컬 계산)
                                sess_now = _session_label()
                                cutoff_rth, cutoff_ext = get_signal_cutoffs()
                                cut_r = cutoff_rth
                                cut_e = cutoff_ext
                                cut = cut_r if sess_now == "RTH" else cut_e
                                if abs(quick_signal.score) < cut:
                                    logger.info(f"🔥 [SCALP DEBUG] 스캘프 신호 억제: {ticker} score={quick_signal.score:.3f} < cut={cut:.3f}")
                                    _record_recent_signal(redis_url=rurl, signal=quick_signal, session_label=sess_now, indicators=indicators, suppressed="below_cutoff")
                                else:
                                    # GPT-5 리스크 pre-check 추가
                                    risk_ok, risk_reason = check_signal_risk_feasibility(quick_signal, sess_now)
                                    if not risk_ok:
                                        logger.warning(f"🛡️ [RISK] 스캘프 신호 리스크 차단: {ticker} - {risk_reason}")
                                        _record_recent_signal(redis_url=rurl, signal=quick_signal, session_label=sess_now, indicators=indicators, suppressed=f"risk_check: {risk_reason}")
                                    else:
                                        logger.info(f"🔥 [SCALP DEBUG] 스캘프 신호 발행: {ticker} {quick_signal.signal_type.value} score={quick_signal.score:.3f} | 리스크: {risk_reason}")
                                        try:
                                            redis_streams.publish_signal({
                                            "ticker": quick_signal.ticker,
                                            "signal_type": quick_signal.signal_type.value,
                                            "score": quick_signal.score,
                                            "confidence": quick_signal.confidence,
                                            "regime": quick_signal.regime,
                                            "tech_score": quick_signal.tech_score,
                                            "sentiment_score": quick_signal.sentiment_score,
                                            "edgar_bonus": quick_signal.edgar_bonus,
                                            "trigger": quick_signal.trigger,
                                            "summary": quick_signal.summary,
                                            "entry_price": quick_signal.entry_price,
                                            "stop_loss": quick_signal.stop_loss,
                                            "take_profit": quick_signal.take_profit,
                                            "horizon_minutes": quick_signal.horizon_minutes,
                                            "timestamp": quick_signal.timestamp.isoformat()
                                        })
                                            logger.info(f"🔥 [SCALP DEBUG] Redis 스트림 발행 성공: {ticker}")
                                            signals_generated += 1
                                            _record_recent_signal(redis_url=rurl, signal=quick_signal, session_label=sess_now, indicators=indicators)
                                            logger.info(f"스캘프 신호: {ticker} {quick_signal.signal_type.value} ({trig_reason}, abs_ret {last_abs_ret:.2%}, range {last_range:.2%})")
                                        except Exception as e:
                                            logger.error(f"🔥 [SCALP DEBUG] 스캘프 Redis 발행 실패: {ticker} - {e}")
                except Exception:
                    pass

                # 2.6 스캘프 모드(대안): 3분봉 3개 연속 양봉이면 롱 신호
                try:
                    if os.getenv("SCALP_3MIN3UP", "false").lower() in ("1", "true", "yes", "on"):
                        bar_sec = int(os.getenv("BAR_SEC", "30") or 30)
                        window = max(1, int(180 / max(bar_sec, 1)))  # 3분 창의 바 개수
                        need = window * 3
                        if len(candles) >= need:
                            w1 = candles[-window:]
                            w2 = candles[-2*window:-window]
                            w3 = candles[-3*window:-2*window]
                            def _is_green(ws):
                                try:
                                    return (ws[-1].c if ws else 0.0) > (ws[0].o if ws else 0.0)
                                except Exception:
                                    return False
                            if _is_green(w1) and _is_green(w2) and _is_green(w3):
                                from app.engine.regime import RegimeType, RegimeResult
                                from app.engine.techscore import TechScoreResult
                                fake_regime = RegimeResult(
                                    regime=RegimeType.TREND,
                                    confidence=0.8,
                                    features={"3min3up": True},
                                    timestamp=datetime.now()
                                )
                                fake_tech = TechScoreResult(
                                    score=1.0,
                                    components={"ema": 0.9, "macd": 0.9, "rsi": 0.8, "vwap": 0.8},
                                    timestamp=datetime.now()
                                )
                                last_px = candles[-1].c
                                quick_signal = signal_mixer.mix_signals(
                                    ticker=ticker,
                                    regime_result=fake_regime,
                                    tech_score=fake_tech,
                                    llm_insight=None,
                                    edgar_filing=None,
                                    current_price=last_px
                                )
                                if quick_signal:
                                    quick_signal.trigger = "3min_3up"
                                    quick_signal.summary = "3min green x3"
                                    sess_now = _session_label()
                                    cutoff_rth, cutoff_ext = get_signal_cutoffs()
                                    cut_r = cutoff_rth
                                    cut_e = cutoff_ext
                                    cut = cut_r if sess_now == "RTH" else cut_e
                                    if abs(quick_signal.score) < cut:
                                        logger.info(f"🔥 [3MIN DEBUG] 3분3상승 신호 억제: {ticker} score={quick_signal.score:.3f} < cut={cut:.3f}")
                                        _record_recent_signal(redis_url=rurl, signal=quick_signal, session_label=sess_now, indicators=indicators, suppressed="below_cutoff")
                                    else:
                                        # 리스크 사전 체크 (GPT-5 권장사항)
                                        risk_ok, risk_reason = check_signal_risk_feasibility(quick_signal, sess_now)
                                        if not risk_ok:
                                            logger.warning(f"🛡️ {ticker} 3분3상승 신호 리스크 차단: {risk_reason}")
                                            _record_recent_signal(redis_url=rurl, signal=quick_signal, session_label=sess_now, indicators=indicators, suppressed="risk_limit")
                                        else:
                                            logger.info(f"🔥 [3MIN DEBUG] 3분3상승 신호 발행: {ticker} long score={quick_signal.score:.3f}")
                                            try:
                                                redis_streams.publish_signal({
                                                "ticker": quick_signal.ticker,
                                                "signal_type": quick_signal.signal_type.value,
                                                "score": quick_signal.score,
                                                "confidence": quick_signal.confidence,
                                                "regime": quick_signal.regime,
                                                "tech_score": quick_signal.tech_score,
                                                "sentiment_score": quick_signal.sentiment_score,
                                                "edgar_bonus": quick_signal.edgar_bonus,
                                                "trigger": quick_signal.trigger,
                                                "summary": quick_signal.summary,
                                                "entry_price": quick_signal.entry_price,
                                                "stop_loss": quick_signal.stop_loss,
                                                "take_profit": quick_signal.take_profit,
                                                "horizon_minutes": quick_signal.horizon_minutes,
                                                "timestamp": quick_signal.timestamp.isoformat()
                                            })
                                                logger.info(f"🔥 [3MIN DEBUG] Redis 스트림 발행 성공: {ticker}")
                                                signals_generated += 1
                                                _record_recent_signal(redis_url=rurl, signal=quick_signal, session_label=sess_now, indicators=indicators)
                                                logger.info(f"스캘프 신호: {ticker} long (3min_3up)")
                                            except Exception as e:
                                                logger.error(f"🔥 [3MIN DEBUG] 3분3상승 Redis 발행 실패: {ticker} - {e}")
                                            continue
                except Exception:
                    pass

                # 3. 레짐 감지
                regime_result = regime_detector.detect_regime(candles)
                
                # 4. 기술적 점수 계산
                tech_score = tech_score_engine.calculate_tech_score(candles)
                
                # 5. EDGAR 공시 확인
                edgar_filing = None
                llm_insight = None
                
                # 최근 EDGAR 공시가 있는지 확인
                recent_edgar = get_recent_edgar_filing(ticker)
                if recent_edgar:
                    edgar_filing = recent_edgar
                    # LLM 게이팅 적용 (EDGAR 이벤트)
                    should_call, call_reason = should_call_llm_for_event(
                        ticker, "edgar", edgar_filing=edgar_filing
                    )
                    if should_call:
                        # 쿼터 소비 및 LLM 분석
                        if consume_llm_call_quota(ticker, "edgar", edgar_filing):
                            llm_insight = llm_engine.analyze_edgar_filing(edgar_filing)
                            logger.info(f"🤖 LLM EDGAR 분석: {ticker} - {call_reason}")
                        else:
                            logger.warning(f"🤖 LLM 쿼터 소비 실패: {ticker} (EDGAR)")
                    else:
                        logger.info(f"🚫 LLM EDGAR 차단: {ticker} - {call_reason}")
                
                # 6. 레짐이 vol_spike인 경우 추가 LLM 분석 (신호 점수 조건부)
                if regime_result.regime.value == 'vol_spike' and llm_engine and not llm_insight:
                    # tech_score를 이용해 신호 점수 체크 (vol_spike 게이팅)
                    should_call, call_reason = should_call_llm_for_event(
                        ticker, "vol_spike", signal_score=tech_score.score
                    )
                    if should_call:
                        # 쿼터 소비 및 LLM 분석
                        if consume_llm_call_quota(ticker, "vol_spike"):
                            text = f"Volatility spike detected for {ticker} in {regime_result.regime.value} regime"
                            llm_insight = llm_engine.analyze_text(text, f"vol_spike_{ticker}", regime='vol_spike')
                            logger.info(f"🤖 LLM vol_spike 분석: {ticker} - {call_reason}")
                        else:
                            logger.warning(f"🤖 LLM 쿼터 소비 실패: {ticker} (vol_spike)")
                    else:
                        logger.info(f"🚫 LLM vol_spike 차단: {ticker} - {call_reason}")
                
                # 6. 세션별 pre-filter (RTH: 일일상한, EXT: 유동성/스프레드/쿨다운/일일상한)
                ext_enabled = (os.getenv("EXTENDED_PRICE_SIGNALS", "false").lower() in ("1","true","yes","on"))
                session_label = _session_label()
                cutoff_rth, cutoff_ext = get_signal_cutoffs()
                dvol5m = float(indicators.get("dollar_vol_5m", 0.0))
                spread_bp = float(indicators.get("spread_bp", 0.0))
                suppress_reason = None
                
                # RTH 일일 상한: '의미있는 신호'에 대해서만 카운트하기 위해
                # 컷오프/리스크 체크를 통과한 이후로 INCR를 이동
                rth_day_key = None
                rth_daily_cap = None
                r_conn = None
                # 중복 카운트 방지용 idempotency 키 구성 요소
                idemp_key = None
                if session_label == "RTH":
                    try:
                        if rurl:
                            r_conn = redis.from_url(rurl)
                            rth_daily_cap = int(os.getenv("RTH_DAILY_CAP", "5"))
                            # ET 기준 날짜 키 (UTC-5, DST 간이 적용)
                            et_tz = timezone(timedelta(hours=-5))
                            now_et = datetime.now(et_tz)
                            # DST 간이 적용 (3월-11월)
                            if 3 <= now_et.month <= 11:
                                et_tz = timezone(timedelta(hours=-4))
                                now_et = datetime.now(et_tz)
                            rth_day_key = f"dailycap:{now_et:%Y%m%d}:RTH:{ticker}"
                            # 90초 idempotency 슬롯키 (중복 카운트 방지)
                            slot = int(now_et.timestamp() // 90)
                            idemp_key = f"cap:idemp:{now_et:%Y%m%d}:{ticker}:{slot}"
                    except Exception as e:
                        logger.warning(f"RTH 일일상한 키 준비 실패: {e}")
                        r_conn = None
                elif session_label in ["EXT", "CLOSED"]:  # CLOSED도 EXT 로직 적용
                    # EXT 세션 판별 및 억제 이유 로그 수집
                    logger.info(f"EXT 세션 체크: {ticker} ext_enabled={ext_enabled} dvol5m={dvol5m:.0f} spread_bp={spread_bp:.1f}")
                    
                    if not ext_enabled:
                        suppress_reason = "ext_disabled"
                        logger.info(f"suppressed=ext_disabled ticker={ticker} session=EXT")
                    elif dvol5m < float(os.getenv("EXT_MIN_DOLLAR_VOL_5M", "50000")):
                        suppress_reason = "low_dvol"
                        logger.info(f"suppressed=low_dvol ticker={ticker} session=EXT dvol5m={dvol5m:.0f} min={os.getenv('EXT_MIN_DOLLAR_VOL_5M', '50000')}")
                    elif spread_bp > float(os.getenv("EXT_MAX_SPREAD_BP", "300")):
                        suppress_reason = "wide_spread"
                        logger.info(f"suppressed=wide_spread ticker={ticker} session=EXT spread_bp={spread_bp:.1f} max={os.getenv('EXT_MAX_SPREAD_BP', '300')}")
                    # 쿨다운/일일 상한 체크: Redis 키 사용 (원자적 처리)
                    try:
                        if rurl and not suppress_reason:  # 이미 억제 사유가 있으면 스킵
                            r = redis.from_url(rurl)
                            cool_min = int(os.getenv("EXT_COOLDOWN_MIN", "7"))
                            daily_cap = int(os.getenv("EXT_DAILY_CAP", "3"))
                            now_ts = int(time.time())
                            cd_key = f"cooldown:{ticker}"
                            
                            # 쿨다운 체크
                            last_ts = int(r.get(cd_key) or 0)
                            if now_ts - last_ts < cool_min * 60:
                                suppress_reason = "cooldown"
                                logger.info(f"EXT 쿨다운: {ticker} ({now_ts - last_ts}s < {cool_min*60}s)")
                            else:
                                # ET 기준 날짜 키
                                et_tz = timezone(timedelta(hours=-5))
                                now_et = datetime.now(et_tz)
                                if 3 <= now_et.month <= 11:
                                    et_tz = timezone(timedelta(hours=-4))
                                    now_et = datetime.now(et_tz)
                                day_key = f"dailycap:{now_et:%Y%m%d}:EXT:{ticker}"
                                
                                # 원자적 체크-증가
                                current_count = r.incr(day_key)
                                r.expire(day_key, 86400)
                                if current_count > daily_cap:
                                    r.decr(day_key)  # 롤백
                                    suppress_reason = "ext_daily_cap"
                                    logger.info(f"EXT 일일상한 초과: {ticker} ({current_count-1}/{daily_cap})")
                                else:
                                    # 통과 시 쿨다운 마킹
                                    r.setex(cd_key, cool_min*60, now_ts)
                    except Exception as e:
                        logger.warning(f"EXT 쿨다운/상한 체크 실패: {e}")
                        pass

                # VaR95 경량 가드: 최근 리턴 샘플 기반(옵션)
                try:
                    from app.engine.risk import rolling_var95
                    # 샘플은 별도 곳에서 채워진다고 가정; 없으면 스킵
                    rurl = os.getenv("REDIS_URL")
                    if rurl:
                        r = redis.from_url(rurl)
                        key = f"risk:rets:{ticker}:{regime_result.regime.value}"
                        samples = [float(x) for x in (r.lrange(key, 0, 9999) or [])]
                        if samples:
                            rolling_var95(samples)
                            # 간이 기준: 예상 손실R>VaR이면 억제
                            # 여기서는 신호 생성 후 컷오프 단계에서 suppress
                            pass
                except Exception:
                    pass

                # 7. 시그널 믹싱
                current_price = candles[-1].c if candles else 0
                signal = signal_mixer.mix_signals(
                    ticker=ticker,
                    regime_result=regime_result,
                    tech_score=tech_score,
                    llm_insight=llm_insight,
                    edgar_filing=edgar_filing,
                    current_price=current_price
                )
                
                if signal:
                    # 컷오프 적용 (세션별)
                    cut = cutoff_rth if session_label == "RTH" else cutoff_ext
                    if abs(signal.score) < cut or suppress_reason:
                        # 억제 사유 통일 형식 로그
                        actual_reason = suppress_reason or "below_cutoff"
                        logger.info(f"suppressed={actual_reason} ticker={ticker} session={session_label} "
                                   f"score={signal.score:.3f} cut={cut:.3f} dvol5m={dvol5m:.0f} spread_bp={spread_bp:.1f}")
                        
                        # 억제 메트릭 누적
                        try:
                            if rurl:
                                r = redis.from_url(rurl)
                                hkey = f"metrics:suppressed:{datetime.utcnow():%Y%m%d}"
                                r.hincrby(hkey, actual_reason, 1)
                        except Exception:
                            pass
                        # 최근 신호 리스트에 suppressed로 기록
                        _record_recent_signal(redis_url=rurl, signal=signal, session_label=session_label, indicators=indicators, suppressed=suppress_reason or "below_cutoff")
                        continue

                    # RTH 일일 상한 체크: 컷오프/리스크 통과 후에 한 번만 적용
                    if session_label == "RTH" and r_conn and rth_day_key and rth_daily_cap is not None:
                        try:
                            # idempotency: 동일 슬롯 내 중복 카운트 방지
                            if idemp_key:
                                if not r_conn.setnx(idemp_key, 1):
                                    # 이미 카운트 처리됨 → 그대로 억제 기록만 후속로 남기고 건너뜀
                                    r_conn.expire(idemp_key, 90)
                                else:
                                    r_conn.expire(idemp_key, 90)
                            current_count = r_conn.incr(rth_day_key)
                            r_conn.expire(rth_day_key, 86400)
                            if current_count > rth_daily_cap:
                                # 상한 초과면 롤백하고 억제 처리로 전환
                                r_conn.decr(rth_day_key)
                                logger.info(f"suppressed=rth_daily_cap ticker={ticker} session={session_label} "
                                            f"score={signal.score:.3f} cut={cut:.3f} dvol5m={dvol5m:.0f} spread_bp={spread_bp:.1f}")
                                _record_recent_signal(redis_url=rurl, signal=signal, session_label=session_label, indicators=indicators, suppressed="rth_daily_cap")
                                continue
                        except Exception as e:
                            logger.warning(f"RTH 일일상한 체크 실패(사후): {e}")

                    # 글로벌 캡(선택): 하루 총 액션 가능 신호 상한
                    try:
                        global_cap = int(os.getenv("GLOBAL_RTH_DAILY_CAP", "0"))
                    except Exception:
                        global_cap = 0
                    if session_label == "RTH" and r_conn and global_cap > 0:
                        try:
                            gkey = f"dailycap:{now_et:%Y%m%d}:RTH:GLOBAL"
                            gcur = r_conn.incr(gkey)
                            r_conn.expire(gkey, 86400)
                            if gcur > global_cap:
                                r_conn.decr(gkey)
                                logger.info(f"suppressed=global_rth_daily_cap ticker={ticker} session={session_label}")
                                _record_recent_signal(redis_url=rurl, signal=signal, session_label=session_label, indicators=indicators, suppressed="global_rth_daily_cap")
                                continue
                        except Exception as e:
                            logger.warning(f"글로벌 RTH 일일상한 체크 실패: {e}")

                    # 방향 락: 최근 반대방향 재진입 금지(테스트 한정)
                    try:
                        lock_sec = int(os.getenv("DIRECTION_LOCK_SEC", "0"))
                    except Exception:
                        lock_sec = 0
                    if lock_sec > 0 and r_conn:
                        try:
                            lkey = f"lock:dir:{ticker}"
                            last = r_conn.get(lkey)
                            if last:
                                last_dir, last_ts = last.decode().split(":")
                                from time import time as _now
                                if last_dir != signal.signal_type.value and (int(_now()) - int(last_ts)) < lock_sec:
                                    logger.info(f"suppressed=direction_lock ticker={ticker} lock={lock_sec}s last={last_dir}")
                                    _record_recent_signal(redis_url=rurl, signal=signal, session_label=session_label, indicators=indicators, suppressed="direction_lock")
                                    continue
                            # 통과 시 현재 방향 기록
                            from time import time as _now
                            r_conn.setex(lkey, lock_sec, f"{signal.signal_type.value}:{int(_now())}")
                        except Exception as e:
                            logger.warning(f"direction_lock 처리 실패: {e}")

                    # 7. Redis 스트림에 발행
                    signal_data = {
                        "ticker": signal.ticker,
                        "signal_type": signal.signal_type.value,
                        "score": signal.score,
                        "confidence": signal.confidence,
                        "regime": signal.regime,
                        "tech_score": signal.tech_score,
                        "sentiment_score": signal.sentiment_score,
                        "edgar_bonus": signal.edgar_bonus,
                        "trigger": signal.trigger,
                        "summary": signal.summary,
                        "entry_price": signal.entry_price,
                        "stop_loss": signal.stop_loss,
                        "take_profit": signal.take_profit,
                        "horizon_minutes": signal.horizon_minutes,
                        "timestamp": signal.timestamp.isoformat()
                    }
                    
                    logger.info(f"🔥 [DEBUG] 시그널 생성됨! ticker={ticker}, type={signal.signal_type.value}, score={signal.score:.3f}, cut={cut:.3f}")
                    
                    # GPT-5 리스크 pre-check 추가
                    risk_ok, risk_reason = check_signal_risk_feasibility(signal, session_label)
                    if not risk_ok:
                        logger.warning(f"🛡️ [RISK] 신호 리스크 차단: {ticker} - {risk_reason}")
                        _record_recent_signal(redis_url=rurl, signal=signal, session_label=session_label, indicators=indicators, suppressed=f"risk_check: {risk_reason}")
                        continue  # 이 신호는 건너뛰고 다음으로
                    
                    logger.info(f"🔥 [DEBUG] 리스크 체크 통과 - Redis 스트림 발행 시도: {ticker} | {risk_reason}")
                    
                    try:
                        redis_streams.publish_signal(signal_data)
                        logger.info(f"🔥 [DEBUG] Redis 스트림 발행 성공: {ticker}")
                        
                        # 바스켓 분석을 위한 신호 스코어 저장 및 히스토리 추가
                        try:
                            rurl = os.getenv("REDIS_URL")
                            if rurl:
                                r = redis.from_url(rurl)
                                now = time.time()
                                
                                # 현재 스코어 저장
                                score_key = f"signal_score:{ticker}"
                                score_data = {
                                    "score": signal.score,
                                    "timestamp": now,
                                    "signal_type": signal.signal_type.value
                                }
                                r.setex(score_key, 300, json.dumps(score_data))  # 5분 TTL
                                
                                # 히스토리 저장 (slope 계산용)
                                history_key = f"score_history:{ticker}"
                                r.lpush(history_key, json.dumps(score_data))
                                r.ltrim(history_key, 0, 19)  # 최근 20개만 유지
                                r.expire(history_key, 1800)  # 30분 TTL
                        except Exception as e:
                            logger.warning(f"신호 스코어 저장 실패: {e}")
                        
                        # Slack 전송: 강신호만 (원래 기획 - 소수·굵직한 알림)
                        if slack_bot:
                            # 강신호 기준: abs(score) >= cut + 0.20 (더 까다롭게)
                            strong_signal_threshold = cut + 0.20
                            is_strong_signal = abs(signal.score) >= strong_signal_threshold
                            
                            if is_strong_signal:
                                logger.info(f"📢 강신호 감지: {ticker} score={signal.score:.3f} >= {strong_signal_threshold:.3f}")
                                
                                # Phase 1.5: 강신호 시 LLM 분석 추가!
                                enhanced_llm_insight = None
                                if llm_engine:
                                    try:
                                        # 강신호를 위한 LLM 분석 프롬프트
                                        strong_signal_text = f"""
강신호 감지: {ticker}
점수: {signal.score:.3f} ({signal.signal_type.value})
레짐: {signal.regime}
기술점수: {signal.tech_score:.3f}
트리거: {signal.trigger}

이 강신호에 대한 상세 분석과 친근한 설명을 제공해주세요.
                                        """
                                        
                                        enhanced_llm_insight = llm_engine.analyze_text(
                                            text=strong_signal_text,
                                            source=f"strong_signal_{ticker}",
                                            edgar_event=False,
                                            regime=signal.regime,
                                            signal_strength=abs(signal.score)  # 강신호 strength 전달!
                                        )
                                        
                                        if enhanced_llm_insight:
                                            logger.info(f"🤖 강신호 LLM 분석 완료: {ticker}")
                                            logger.info(f"  - 감성: {enhanced_llm_insight.sentiment:.2f}")
                                            logger.info(f"  - 트리거: {enhanced_llm_insight.trigger}")
                                            logger.info(f"  - 요약: {enhanced_llm_insight.summary}")
                                        
                                    except Exception as llm_e:
                                        logger.warning(f"강신호 LLM 분석 실패: {ticker} - {llm_e}")
                                
                                try:
                                    # 기존 메시지나 LLM 향상된 메시지 사용
                                    if enhanced_llm_insight:
                                        # LLM 분석이 있으면 더 친근한 메시지로 포맷
                                        # Phase 1.5: 신호 검증 시스템 적용!
                                        validated_signal = signal
                                        try:
                                            from app.jobs.signal_validation import SignalValidationEngine
                                            validation_engine = SignalValidationEngine()
                                            validation_result = validation_engine.validate_signal(signal)
                                            
                                            if validation_result:
                                                validated_signal = validation_engine.apply_validation_result(signal, validation_result)
                                                logger.info(f"🔍 신호 검증 완료: {ticker} - {'✅통과' if validation_result.should_proceed else '🛑거부'}")
                                                
                                                # 검증 거부된 신호는 전송하지 않음
                                                if not validation_result.should_proceed:
                                                    logger.info(f"suppressed=validation_rejected ticker={ticker} reason={validation_result.validation_reason}")
                                                    continue
                                        except Exception as val_e:
                                            logger.warning(f"신호 검증 실패: {ticker} - {val_e}")
                                        
                                        slack_message = format_enhanced_slack_message(validated_signal, enhanced_llm_insight)
                                    else:
                                        # 기존 메시지 사용
                                        slack_message = format_slack_message(signal)
                                    result = slack_bot.send_message(slack_message)
                                    if result:
                                        logger.info(f"✅ Slack 전송 성공: {ticker} (카운터는 이미 전치 체크에서 증가됨)")
                                        
                                        # Phase 1.5: 페이퍼 트레이딩 자동 실행!
                                        try:
                                            from app.jobs.paper_trading_manager import get_paper_trading_manager
                                            paper_manager = get_paper_trading_manager()
                                            
                                            execution_result = paper_manager.execute_signal(validated_signal)
                                            if execution_result:
                                                logger.info(f"📊 페이퍼 트레이딩 실행: {ticker}")
                                            else:
                                                logger.info(f"📊 페이퍼 트레이딩 스킵: {ticker}")
                                        except Exception as paper_e:
                                            logger.warning(f"페이퍼 트레이딩 실행 실패: {ticker} - {paper_e}")
                                    else:
                                        # Slack 전송 실패 시 카운터 롤백 (전치 체크에서 이미 증가했으므로)
                                        try:
                                            if rurl:
                                                r = redis.from_url(rurl)
                                                if session_label == "RTH":
                                                    et_tz = timezone(timedelta(hours=-5))
                                                    now_et = datetime.now(et_tz)
                                                    if 3 <= now_et.month <= 11:
                                                        et_tz = timezone(timedelta(hours=-4))
                                                        now_et = datetime.now(et_tz)
                                                    day_key = f"dailycap:{now_et:%Y%m%d}:RTH:{ticker}"
                                                    r.decr(day_key)
                                                elif session_label == "EXT":
                                                    et_tz = timezone(timedelta(hours=-5))
                                                    now_et = datetime.now(et_tz)
                                                    if 3 <= now_et.month <= 11:
                                                        et_tz = timezone(timedelta(hours=-4))
                                                        now_et = datetime.now(et_tz)
                                                    day_key = f"dailycap:{now_et:%Y%m%d}:EXT:{ticker}"
                                                    r.decr(day_key)
                                                logger.info(f"Slack 전송 실패로 카운터 롤백: {ticker}")
                                        except Exception as e:
                                            logger.warning(f"카운터 롤백 실패: {e}")
                                        logger.error(f"❌ Slack 전송 실패: {ticker}")
                                except Exception as e:
                                    # 예외 발생 시에도 카운터 롤백
                                    try:
                                        if rurl:
                                            r = redis.from_url(rurl)
                                            if session_label == "RTH":
                                                et_tz = timezone(timedelta(hours=-5))
                                                now_et = datetime.now(et_tz)
                                                if 3 <= now_et.month <= 11:
                                                    et_tz = timezone(timedelta(hours=-4))
                                                    now_et = datetime.now(et_tz)
                                                day_key = f"dailycap:{now_et:%Y%m%d}:RTH:{ticker}"
                                                r.decr(day_key)
                                            elif session_label == "EXT":
                                                et_tz = timezone(timedelta(hours=-5))
                                                now_et = datetime.now(et_tz)
                                                if 3 <= now_et.month <= 11:
                                                    et_tz = timezone(timedelta(hours=-4))
                                                    now_et = datetime.now(et_tz)
                                                day_key = f"dailycap:{now_et:%Y%m%d}:EXT:{ticker}"
                                                r.decr(day_key)
                                            logger.info(f"Slack 전송 예외로 카운터 롤백: {ticker}")
                                    except Exception as rollback_e:
                                        logger.warning(f"카운터 롤백 실패: {rollback_e}")
                                    logger.error(f"❌ Slack 전송 예외: {ticker} - {e}")
                            else:
                                logger.info(f"🔇 Slack 전송 억제 (약신호): {ticker} score={signal.score:.3f} < {strong_signal_threshold:.3f}")
                        else:
                            logger.warning(f"🔍 Slack 전송 건너뜀 - slack_bot이 None: {ticker}")
                        
                        signals_generated += 1
                    except Exception as e:
                        logger.error(f"🔥 [DEBUG] Redis 스트림 발행 실패: {ticker} - {e}")
                        continue
                    # 최근 신호 기록 (+ 세션/스프레드/달러대금)
                    _record_recent_signal(redis_url=rurl, signal=signal, session_label=session_label, indicators=indicators)
                    
                    tier_info = f" [Tier:{tier.value}]" if tier else ""
                    logger.info(f"시그널 생성: {ticker} {signal.signal_type.value} (점수: {signal.score:.2f}){tier_info}")
                
            except Exception as e:
                logger.error(f"시그널 생성 실패 ({ticker}): {e}")
                continue
        
        execution_time = time.time() - start_time
        
        # 토큰 사용량 로그 (Tier 시스템)
        try:
            rate_limiter = get_rate_limiter()
            token_status = rate_limiter.get_token_status()
            api_usage = rate_limiter.get_total_api_usage()
            logger.info(f"📊 토큰 상태: A={token_status['tier_a']['current_tokens']}/{token_status['tier_a']['max_tokens']}, "
                       f"B={token_status['tier_b']['current_tokens']}/{token_status['tier_b']['max_tokens']}, "
                       f"예약={token_status['reserve']['current_tokens']}/{token_status['reserve']['max_tokens']} "
                       f"(사용률: {api_usage['usage_pct']:.1f}%)")
        except Exception as e:
            logger.warning(f"토큰 상태 조회 실패: {e}")
        
        # LLM 사용량 로그 (게이팅 시스템)
        try:
            llm_stats = get_llm_usage_stats()
            logger.info(f"🤖 LLM 사용량: {llm_stats['daily_used']}/{llm_stats['daily_limit']} "
                       f"(남은 호출: {llm_stats['remaining']}, 사용률: {llm_stats['usage_pct']:.1f}%)")
        except Exception as e:
            logger.warning(f"LLM 사용량 조회 실패: {e}")
        
        logger.info(f"시그널 생성 완료: {signals_generated}개, {execution_time:.2f}초")
        
        return {
            "status": "success",
            "signals_generated": signals_generated,
            "execution_time": execution_time,
            "timestamp": datetime.now().isoformat()
        }
        
    except Exception as e:
        logger.error(f"시그널 생성 작업 실패: {e}")
        return {
            "status": "error",
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }

@celery_app.task(bind=True, name="app.jobs.scheduler.update_quotes")
def update_quotes(self):
    """시세 업데이트 작업"""
    try:
        start_time = time.time()
        logger.debug("시세 업데이트 시작")
        
        quotes_ingestor = trading_components["quotes_ingestor"]
        redis_streams = trading_components["redis_streams"]
        
        if not quotes_ingestor or not redis_streams:
            return {"status": "skipped", "reason": "components_not_ready"}
        
        # 모든 종목 시세 업데이트
        quotes_ingestor.update_all_tickers()
        
        # Redis 스트림에 발행
        market_data = quotes_ingestor.get_market_data_summary()
        # 옵션: 야후 인제스터 요약 로그 (실시간 틱 추적용)
        try:
            if os.getenv("QUOTE_LOG_VERBOSE", "false").lower() in ("1", "true", "yes", "on"):
                for _t, _d in (market_data or {}).items():
                    _ind = _d.get("indicators", {})
                    _px = float(_d.get("current_price", 0.0) or 0.0)
                    _dv = float(_ind.get("dollar_vol_5m", 0.0) or 0.0)
                    _sp = float(_ind.get("spread_bp", 0.0) or 0.0)
                    _ts = _d.get("last_update")
                    logger.info(f"[YQ] { _t } px={_px:.4f} dollar_vol_5m={_dv:.0f} spread_bp={_sp:.1f} ts={_ts}")
        except Exception:
            pass
        for ticker, data in market_data.items():
            if data.get("current_price"):
                quote_data = {
                    "ticker": ticker,
                    "price": data["current_price"],
                    "indicators": data.get("indicators", {}),
                    "timestamp": data.get("last_update", datetime.now()).isoformat()
                }
                redis_streams.publish_quote(ticker, "XNAS", quote_data)
        
        execution_time = time.time() - start_time
        logger.debug(f"시세 업데이트 완료: {len(market_data)}개 종목, {execution_time:.2f}초")
        
        return {
            "status": "success",
            "tickers_updated": len(market_data),
            "execution_time": execution_time,
            "timestamp": datetime.now().isoformat()
        }
        
    except Exception as e:
        logger.error(f"시세 업데이트 작업 실패: {e}")
        return {
            "status": "error",
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }

@celery_app.task(bind=True, name="app.jobs.scheduler.scan_edgar")
def scan_edgar(self):
    """EDGAR 스캔 작업"""
    try:
        start_time = time.time()
        logger.debug("EDGAR 스캔 시작")
        
        # EDGAR 스캐너 준비
        try:
            from app.io.edgar import EDGARScanner
        except Exception:
            EDGARScanner = None  # type: ignore
        edgar_scanner = trading_components.get("edgar_scanner")
        if edgar_scanner is None and EDGARScanner is not None:
            edgar_scanner = EDGARScanner()
            trading_components["edgar_scanner"] = edgar_scanner
        redis_streams = trading_components.get("redis_streams")
        redis_client = None
        try:
            # dedupe를 위한 raw redis 클라이언트 접근 (streams 내부 클라이언트 재사용)
            if redis_streams:
                redis_client = redis_streams.redis_client
        except Exception:
            redis_client = None
        
        if not redis_streams:
            return {"status": "skipped", "reason": "components_not_ready"}
        
        # EDGAR 공시 스캔
        filings = edgar_scanner.run_scan()
        
        # Redis 스트림에 발행 (중복 방지)
        dedupe_key = "edgar:dedupe:snippets"
        published = 0
        for filing in filings:
            snippet_text = filing.get("summary") or filing.get("snippet_text") or ""
            url = filing.get("url", "")
            base = (snippet_text or url).encode()
            snippet_hash = hashlib.md5(base).hexdigest()
            if redis_client:
                # 중복이면 스킵
                added = redis_client.sadd(dedupe_key, snippet_hash)
                if not added:
                    continue
            # 해시를 함께 저장해두기
            filing["snippet_hash"] = snippet_hash
            # Streams는 문자열 값이 안전하므로 dict/list는 JSON 문자열로 변환
            payload = {k: json.dumps(v) if isinstance(v, (dict, list)) else (v if v is not None else "") for k, v in filing.items()}
            redis_streams.publish_edgar(payload)
            published += 1
            
            # 중요 공시 LLM 처리
            llm_engine = trading_components.get("llm_engine")
            if filing.get("impact_score", 0) > 0.7 and llm_engine:
                llm_insight = llm_engine.analyze_edgar_filing(filing)
                if llm_insight:
                    insight_data = {
                        "ticker": filing["ticker"],
                        "sentiment": llm_insight.sentiment,
                        "trigger": llm_insight.trigger,
                        "horizon_minutes": llm_insight.horizon_minutes,
                        "summary": llm_insight.summary,
                        "timestamp": llm_insight.timestamp.isoformat()
                    }
                    redis_streams.publish_news(insight_data)
        
        execution_time = time.time() - start_time
        logger.debug(f"EDGAR 스캔 완료: {published}개 발행, {execution_time:.2f}초")
        
        return {
            "status": "success",
            "published": published,
            "execution_time": execution_time,
            "timestamp": datetime.now().isoformat()
        }
        
    except Exception as e:
        logger.error(f"EDGAR 스캔 작업 실패: {e}")
        return {
            "status": "error",
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }

@celery_app.task(bind=True, name="app.jobs.scheduler.check_risk")
def check_risk(self):
    """리스크 체크 작업"""
    try:
        start_time = time.time()
        logger.debug("리스크 체크 시작")
        
        risk_engine = trading_components["risk_engine"]
        slack_bot = trading_components["slack_bot"]
        redis_streams = trading_components["redis_streams"]
        
        if not risk_engine:
            return {"status": "skipped", "reason": "components_not_ready"}
        
        # 리스크 지표 계산
        risk_metrics = risk_engine.calculate_risk_metrics()
        
        # Redis 스트림에 발행
        if redis_streams:
            risk_data = {
                "daily_pnl": risk_metrics.daily_pnl,
                "daily_pnl_pct": risk_metrics.daily_pnl_pct,
                "var_95": risk_metrics.var_95,
                "max_drawdown": risk_metrics.max_drawdown,
                "position_count": risk_metrics.position_count,
                "total_exposure": risk_metrics.total_exposure,
                "status": risk_metrics.status.value,
                "timestamp": risk_metrics.timestamp.isoformat()
            }
            redis_streams.publish_risk_update(risk_data)
        
        # 경고/위험 상태일 때 Slack 알림
        if risk_metrics.status.value in ["warning", "critical", "shutdown"] and slack_bot:
            risk_report = risk_engine.get_risk_report()
            slack_bot.send_risk_alert(risk_report)
        
        execution_time = time.time() - start_time
        logger.debug(f"리스크 체크 완료: {risk_metrics.status.value}, {execution_time:.2f}초")
        
        return {
            "status": "success",
            "risk_status": risk_metrics.status.value,
            "daily_pnl_pct": risk_metrics.daily_pnl_pct,
            "execution_time": execution_time,
            "timestamp": datetime.now().isoformat()
        }
        
    except Exception as e:
        logger.error(f"리스크 체크 작업 실패: {e}")
        return {
            "status": "error",
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }

@celery_app.task(bind=True, name="app.jobs.scheduler.daily_reset")
def daily_reset(self):
    """일일 리셋 작업"""
    try:
        logger.info("일일 리셋 시작")
        
        risk_engine = trading_components["risk_engine"]
        paper_ledger = trading_components["paper_ledger"]
        slack_bot = trading_components["slack_bot"]
        
        # 리스크 엔진 리셋
        if risk_engine:
            risk_engine.reset_daily()
        
        # 페이퍼 레저 리셋
        if paper_ledger:
            paper_ledger.reset_daily()
        
        # Slack 알림
        if slack_bot:
            message = {
                "text": "🔄 일일 리셋이 완료되었습니다",
                "channel": "#trading-signals"
            }
            slack_bot.send_message(message)
        
        logger.info("일일 리셋 완료")
        
        return {
            "status": "success",
            "message": "일일 리셋 완료",
            "timestamp": datetime.now().isoformat()
        }
        
    except Exception as e:
        logger.error(f"일일 리셋 작업 실패: {e}")
        return {
            "status": "error",
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }

@celery_app.task(name="app.jobs.scheduler.daily_report")
def daily_report(force=False, post=True):
    """일일 리포트 작업"""
    try:
        logger.info("일일 리포트 생성 시작")
        
        paper_ledger = trading_components["paper_ledger"]
        risk_engine = trading_components["risk_engine"]
        llm_engine = trading_components["llm_engine"]
        slack_bot = trading_components["slack_bot"]
        
        if not paper_ledger:
            return {"status": "skipped", "reason": "components_not_ready"}
        
        # 일일 통계 수집
        daily_stats = paper_ledger.get_daily_stats()
        
        # 리스크 지표
        risk_metrics = None
        if risk_engine:
            risk_metrics = risk_engine.get_risk_report()
        
        # LLM 사용량
        llm_usage = None
        if llm_engine:
            llm_usage = llm_engine.get_status()
        
        # 리포트 데이터 구성
        report_data = {
            "trades": daily_stats.get("trades", 0),
            "realized_pnl": daily_stats.get("realized_pnl", 0),
            "win_rate": 0.6,  # 실제로는 계산 필요
            "avg_rr": 1.4,    # 실제로는 계산 필요
            "risk_metrics": risk_metrics,
            "llm_usage": llm_usage
        }
        
        # Slack 리포트 전송
        if slack_bot and post:
            slack_bot.send_daily_report(report_data)
        
        logger.info("일일 리포트 생성 완료")
        
        return {
            "status": "success",
            "trades": report_data["trades"],
            "realized_pnl": report_data["realized_pnl"],
            "timestamp": datetime.now().isoformat()
        }
        
    except Exception as e:
        logger.error(f"일일 리포트 작업 실패: {e}")
        return {
            "status": "error",
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }

def get_recent_edgar_filing(ticker: str) -> Optional[Dict]:
    """최근 EDGAR 공시 조회 (캐시된 데이터에서)"""
    # 실제로는 캐시나 데이터베이스에서 조회
    # 여기서는 간단히 None 반환
    return None

def _session_label() -> str:
    """RTH/EXT 간단 판별: ET 시간으로 09:30-16:00은 RTH, 그 외 04:00-20:00은 EXT, 나머지는 CLOSED"""
    from datetime import datetime, timedelta, time as dtime, timezone
    et_tz = timezone(timedelta(hours=-5))
    now_est = datetime.now(et_tz)
    # DST 간이 적용
    dst_start = now_est.replace(month=3, day=8 + (6 - now_est.replace(month=3, day=1).weekday()) % 7, hour=2)
    dst_end = now_est.replace(month=11, day=1 + (6 - now_est.replace(month=11, day=1).weekday()) % 7, hour=2)
    if dst_start <= now_est < dst_end:
        et_tz = timezone(timedelta(hours=-4))
        
    now = datetime.now(et_tz).time()
    session = None
    if dtime(9,30) <= now <= dtime(16,0):
        session = "RTH"
    elif dtime(4,0) <= now <= dtime(20,0):
        session = "EXT"
    else:
        session = "CLOSED"
    
    # 세션 판별 로그 (디버그 목적)
    logger.debug(f"session={session} et_time={now} dst_active={dst_start <= now_est < dst_end}")
    return session

def _record_recent_signal(redis_url: Optional[str], signal, session_label: str, indicators: Dict, suppressed: Optional[str] = None) -> None:
    try:
        if not redis_url:
            return
        r = redis.from_url(redis_url)
        key = "signals:recent"
        payload = {
            "ticker": signal.ticker,
            "signal_type": signal.signal_type.value,
            "score": signal.score,
            "confidence": signal.confidence,
            "regime": signal.regime,
            "timestamp": signal.timestamp.isoformat(),
            "session": session_label,
            "spread_bp": float(indicators.get("spread_bp", 0.0)),
            "dollar_vol_5m": float(indicators.get("dollar_vol_5m", 0.0)),
        }
        if suppressed:
            payload["suppressed_reason"] = suppressed
        r.lpush(key, json.dumps(payload))
        r.ltrim(key, 0, 500)
    except Exception:
        pass

def initialize_components(components: Dict):
    """컴포넌트 초기화"""
    global trading_components
    trading_components.update(components)
    logger.info("스케줄러 컴포넌트 초기화 완료")

    # Quotes ingestor 기본 주입(딜레이드)
    try:
        from app.io.quotes_delayed import DelayedQuotesIngestor
        if trading_components.get("quotes_ingestor") is None:
            trading_components["quotes_ingestor"] = DelayedQuotesIngestor()
            logger.info("딜레이드 Quotes 인제스터 초기화")
    except Exception as e:
        logger.warning(f"Quotes 인제스터 초기화 실패: {e}")


@celery_app.task(bind=True, name="app.jobs.scheduler.ingest_edgar_stream")
def ingest_edgar_stream(self):
    """Redis Streams(news.edgar) → DB(edgar_events) 적재. DB UNIQUE(snippet_hash)로 최종 dedupe.
    워커 부팅 후 readiness 통과 시 주기적으로 실행된다.
    """
    try:
        from app.io.streams import RedisStreams, StreamConsumer
        rs = trading_components.get("redis_streams")
        if rs is None:
            # env에서 연결
            import urllib.parse as _u
            redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")
            parsed = _u.urlparse(redis_url)
            host = parsed.hostname or "redis"
            port = int(parsed.port or 6379)
            db = int(parsed.path.lstrip("/") or 0)
            rs = RedisStreams(host=host, port=port, db=db)
            trading_components["redis_streams"] = rs
        consumer = trading_components.get("stream_consumer") or StreamConsumer(rs)
        trading_components["stream_consumer"] = consumer

        # DB 연결 확보/재사용
        conn = trading_components.get("db_connection")
        if conn is None or conn.closed:
            dsn = os.getenv("POSTGRES_URL") or os.getenv("DATABASE_URL")
            if not dsn:
                return {"status": "skipped", "reason": "missing_db_dsn", "timestamp": datetime.now().isoformat()}
            conn = psycopg2.connect(dsn)
            conn.autocommit = True
            trading_components["db_connection"] = conn

        messages = consumer.consume_edgar_events(count=20, block_ms=100)
        inserted = 0
        with conn.cursor() as cur:
            for msg in messages:
                d = msg.data
                # 문자열로 온 JSON을 원상복구 시도
                def _norm(key):
                    val = d.get(key)
                    if val is None:
                        return None
                    try:
                        return json.loads(val)
                    except Exception:
                        return val

                ticker = _norm("ticker") or d.get("ticker")
                form = _norm("form") or d.get("form")
                item = _norm("item") or d.get("item")
                url = _norm("url") or d.get("url")
                snippet_text = _norm("snippet_text") or d.get("snippet_text")
                snippet_hash = _norm("snippet_hash") or d.get("snippet_hash")
                
                # form이 None이면 snippet_text에서 추출 시도
                if form is None and snippet_text:
                    import re
                    match = re.search(r'\b(4|8-K|10-K|10-Q|S-1|S-3|DEF 14A)\b', str(snippet_text))
                    if match:
                        form = match.group(1)

                cur.execute(
                    """
                    INSERT INTO edgar_events (ticker, form, item, url, snippet_hash, snippet_text)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (snippet_hash) DO NOTHING
                    """,
                    (str(ticker) if ticker is not None else None,
                     str(form) if form is not None else None,
                     str(item) if item is not None else None,
                     str(url) if url is not None else None,
                     str(snippet_hash) if snippet_hash is not None else None,
                     str(snippet_text) if snippet_text is not None else None)
                )
                consumer.acknowledge("news.edgar", msg.message_id)
                inserted += 1

        return {"status": "success", "inserted": inserted, "timestamp": datetime.now().isoformat()}
    except Exception as e:
        logger.error(f"EDGAR 스트림 적재 실패: {e}")
        return {"status": "error", "error": str(e), "timestamp": datetime.now().isoformat()}

def get_task_status(task_id: str) -> Dict:
    """작업 상태 조회"""
    try:
        result = celery_app.AsyncResult(task_id)
        return {
            "task_id": task_id,
            "status": result.status,
            "result": result.result if result.ready() else None,
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        logger.error(f"작업 상태 조회 실패: {e}")
        return {
            "task_id": task_id,
            "status": "error",
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }

@celery_app.task(name="app.jobs.scheduler.refresh_universe")
def refresh_universe():
    """15분마다 유니버스 병합→인제스터 반영"""
    try:
        quotes_ingestor = trading_components.get("quotes_ingestor")
        rurl = os.getenv("REDIS_URL")
        if not quotes_ingestor or not rurl:
            return {"status": "skipped"}
        r = redis.from_url(rurl)
        external = r.smembers("universe:external") or []
        watch = r.smembers("universe:watchlist") or []
        core = [t.strip().upper() for t in (os.getenv("TICKERS", "").split(",")) if t.strip()]
        ext = [x.decode() if isinstance(x, (bytes, bytearray)) else x for x in external]
        wch = [x.decode() if isinstance(x, (bytes, bytearray)) else x for x in watch]
        merged = []
        seen = set()
        max_n = int(os.getenv("UNIVERSE_MAX", "60"))
        for arr in [core, ext, wch]:
            for s in arr:
                if s and s not in seen:
                    merged.append(s)
                    seen.add(s)
                if len(merged) >= max_n:
                    break
            if len(merged) >= max_n:
                break
        if hasattr(quotes_ingestor, "update_universe_tickers"):
            quotes_ingestor.update_universe_tickers(merged)
        return {"status": "ok", "universe_size": len(merged)}
    except Exception as e:
        logger.error(f"유니버스 갱신 실패: {e}")
        return {"status": "error", "error": str(e)}

@celery_app.task(name="app.jobs.scheduler.adaptive_cutoff")
def adaptive_cutoff():
    """전일 체결 건수 기반 컷오프 조정: cfg:signal_cutoff:{rth|ext} ±0.02 with bounds"""
    try:
        if os.getenv("ADAPTIVE_CUTOFF_ENABLED", "true").lower() not in ("1","true","yes","on"):
            return {"status": "skipped", "reason": "disabled"}
        rurl = os.getenv("REDIS_URL")
        if not rurl:
            return {"status": "skipped", "reason": "no_redis"}
        r = redis.from_url(rurl)
        # 체결 건수: 간이 집계(orders_paper 테이블 없으면 0 처리)
        fills = 0
        try:
            dsn = os.getenv("POSTGRES_URL") or os.getenv("DATABASE_URL")
            if dsn:
                import psycopg2
                from datetime import timedelta
                conn = psycopg2.connect(dsn)
                cur = conn.cursor()
                prev = (datetime.utcnow() - timedelta(days=1)).date()
                cur.execute("SELECT COUNT(*) FROM orders_paper WHERE DATE(ts)=%s", (prev,))
                fills = int(cur.fetchone()[0] or 0)
                cur.close()
                conn.close()
        except Exception:
            fills = 0
        # 현재값 읽기
        def _getf(k, d):
            v = r.get(k)
            try:
                return float(v) if v is not None else d
            except Exception:
                return d
        rth = _getf("cfg:signal_cutoff:rth", settings.SIGNAL_CUTOFF_RTH)
        ext = _getf("cfg:signal_cutoff:ext", settings.SIGNAL_CUTOFF_EXT)
        # 조정 (현실적 경계값으로 수정)
        rth_base, ext_base = settings.SIGNAL_CUTOFF_RTH, settings.SIGNAL_CUTOFF_EXT
        delta = -0.02 if fills == 0 else (0.02 if fills >= 4 else 0.0)
        rth_new = min(max(rth + delta, max(0.12, rth_base - 0.06)), rth_base + 0.12)
        ext_new = min(max(ext + delta, max(0.18, ext_base - 0.10)), ext_base + 0.10)
        r.set("cfg:signal_cutoff:rth", rth_new)
        r.set("cfg:signal_cutoff:ext", ext_new)
        return {"status": "ok", "fills": fills, "rth": rth_new, "ext": ext_new}
    except Exception as e:
        logger.error(f"적응형 컷오프 실패: {e}")
        return {"status": "error", "error": str(e)}


if __name__ == "__main__":
    # 개발용 실행
    celery_app.start()
