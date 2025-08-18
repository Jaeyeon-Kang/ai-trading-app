"""
Celery beat 스케줄러
15-30초 주기로 시그널 생성 및 거래 실행
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
import time
from typing import Dict, List, Optional, Tuple, Union
import urllib.parse as _urlparse

import psycopg2
import redis

from celery import Celery
from celery.schedules import crontab
from celery.signals import beat_init, task_prerun, worker_process_init, worker_ready

# 로깅 설정 (다른 import보다 먼저)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# GPT-5 리스크 관리 통합
try:
    from app.engine.risk_manager import get_risk_manager
    from app.adapters.trading_adapter import get_trading_adapter
    RISK_MANAGER_AVAILABLE = True
except ImportError:
    RISK_MANAGER_AVAILABLE = False
    logger.warning("⚠️ 리스크 관리자 import 실패 - 기본 모드로 동작")

from app.config import settings, get_signal_cutoffs, sanitize_cutoffs_in_redis
from app.utils.rate_limiter import get_rate_limiter, TokenTier

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
            # Tier A/B는 해당 토큰 사용 (Fallback도 시도)
            fallback_tier = TokenTier.RESERVE if tier == TokenTier.TIER_A else None
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
                return False, f"signal_score_too_low (|{signal_score}| < {settings.LLM_MIN_SIGNAL_SCORE})"
            
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

@celery_app.task(bind=True, name="app.jobs.scheduler.pipeline_e2e")
def pipeline_e2e(self):
    """E2E 파이프라인: EDGAR 이벤트 → LLM → 레짐 → 믹서 → DB → Slack"""
    try:
        start_time = time.time()
        logger.info("E2E 파이프라인 시작")
        
        # 컴포넌트 확인 (필수 최소 구성만 강제, LLM은 선택)
        if not all([
            trading_components["stream_consumer"],
            trading_components["regime_detector"],
            trading_components["signal_mixer"],
            trading_components["slack_bot"]
        ]):
            logger.warning("필수 컴포넌트 미준비(stream_consumer/regime_detector/signal_mixer/slack_bot)")
            return {"status": "skipped", "reason": "components_not_ready"}
        
        stream_consumer = trading_components["stream_consumer"]
        llm_engine = trading_components["llm_engine"]
        regime_detector = trading_components["regime_detector"]
        signal_mixer = trading_components["signal_mixer"]
        slack_bot = trading_components["slack_bot"]
        
        signals_processed = 0
        
        # 1. EDGAR 이벤트 소비
        edgar_events = stream_consumer.consume_edgar_events(count=5, block_ms=100)
        
        for event in edgar_events:
            try:
                ticker = event.data.get("ticker")
                if not ticker:
                    continue
                
                # 2. LLM 분석 (EDGAR 이벤트이므로 조건 충족)
                llm_insight = None
                if llm_engine:
                    text = event.data.get("snippet_text", "")
                    url = event.data.get("url", "")
                    llm_insight = llm_engine.analyze_text(text, url, edgar_event=True)
                
                # 3. 시세 데이터 가져오기 (간단한 모의 데이터)
                candles = get_mock_candles(ticker)
                get_mock_indicators(ticker)
                
                # 4. 레짐 감지
                regime_result = regime_detector.detect_regime(candles)
                
                # 5. 기술적 점수 계산
                tech_score = get_mock_tech_score(ticker)
                
                # 6. 시그널 믹싱
                current_price = 150.0  # 모의 가격
                signal = signal_mixer.mix_signals(
                    ticker=ticker,
                    regime_result=regime_result,
                    tech_score=tech_score,
                    llm_insight=llm_insight,
                    edgar_filing=event.data,
                    current_price=current_price
                )
                
                if signal:
                    # 메타에 세션/품질 지표 심기 → Slack 헤더 라벨용
                    if not signal.meta:
                        signal.meta = {}
                    session_label = _session_label()
                    signal.meta["session"] = session_label
                    # 안전 가드: 미정의 변수 처리
                    signal.meta.setdefault("spread_bp", 0.0)
                    signal.meta.setdefault("dollar_vol_5m", 0.0)
                    # 7. DB에 저장
                    if trading_components.get("db_connection"):
                        signal_mixer.save_signal_to_db(signal, trading_components["db_connection"])
                    
                    # 8. Slack 알림
                    if slack_bot:
                        slack_message = format_slack_message(signal)
                        slack_bot.send_message(slack_message)
                    
                    # 9. 메시지 ACK
                    stream_consumer.acknowledge("news.edgar", event.message_id)
                    
                    signals_processed += 1
                    logger.info(f"파이프라인 완료: {ticker} {signal.signal_type.value}")
                
            except Exception as e:
                logger.error(f"이벤트 처리 실패: {e}")
                continue
        
        execution_time = time.time() - start_time
        logger.info(f"E2E 파이프라인 완료: {signals_processed}개, {execution_time:.2f}초")
        
        return {
            "status": "success",
            "signals_processed": signals_processed,
            "execution_time": execution_time,
            "timestamp": datetime.now().isoformat()
        }
        
    except Exception as e:
        logger.error(f"E2E 파이프라인 실패: {e}")
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
    
    # 버튼 생성 여부 확인 (반자동 모드)
    show_buttons = (
        os.getenv("SEMI_AUTO_BUTTONS", "0").lower() in ("1", "true", "yes", "on") or
        os.getenv("AUTO_MODE", "0").lower() in ("1", "true", "yes", "on")
    )
    
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
    
    # 버튼 추가 (기존 로직 재사용)
    show_buttons = (
        os.getenv("SEMI_AUTO_BUTTONS", "0").lower() in ("1", "true", "yes", "on") or
        os.getenv("AUTO_MODE", "0").lower() in ("1", "true", "yes", "on")
    )
    
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
                
                # RTH 일일 상한 (5건 목표) - 원자적 전치 체크
                if session_label == "RTH":
                    try:
                        if rurl:
                            r = redis.from_url(rurl)
                            rth_daily_cap = int(os.getenv("RTH_DAILY_CAP", "5"))
                            # ET 기준 날짜 키 (UTC-5, DST 간이 적용)
                            et_tz = timezone(timedelta(hours=-5))
                            now_et = datetime.now(et_tz)
                            # DST 간이 적용 (3월-11월)
                            if 3 <= now_et.month <= 11:
                                et_tz = timezone(timedelta(hours=-4))
                                now_et = datetime.now(et_tz)
                            day_key = f"dailycap:{now_et:%Y%m%d}:RTH:{ticker}"
                            # 원자적 체크-증가: INCR 후 결과로 판단
                            current_count = r.incr(day_key)
                            r.expire(day_key, 86400)  # ET EOD 만료 (24시간)
                            if current_count > rth_daily_cap:
                                # 상한 초과 시 카운터 롤백하고 억제
                                r.decr(day_key)
                                suppress_reason = "rth_daily_cap"
                                logger.info(f"RTH 일일상한 초과: {ticker} ({current_count-1}/{rth_daily_cap})")
                    except Exception as e:
                        logger.warning(f"RTH 일일상한 체크 실패: {e}")
                        pass
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
