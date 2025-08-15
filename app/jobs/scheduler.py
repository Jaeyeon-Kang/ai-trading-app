"""
Celery beat 스케줄러
15-30초 주기로 시그널 생성 및 거래 실행
"""
from datetime import datetime, timedelta
import hashlib
import json
import logging
import os
import time
from typing import Dict, List, Optional
import urllib.parse as _urlparse

import psycopg2
import redis

from celery import Celery
from celery.schedules import crontab
from celery.signals import beat_init, task_prerun, worker_process_init, worker_ready

from app.config import settings, get_signal_cutoffs, sanitize_cutoffs_in_redis

# 로깅 설정
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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
                indicators = get_mock_indicators(ticker)
                
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
    """Slack 메시지 포맷"""
    return {
        "channel": "C099CQP8CJ3",  # 🔧 명시적으로 채널 ID 지정 (channel_not_found 오류 해결)
        "text": (
            f"{signal.ticker} | 레짐 {signal.regime.upper()}({signal.confidence:.2f}) | "
            f"점수 {signal.score:+.2f} {'롱' if signal.signal_type.value == 'long' else '숏'}"
        )
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
        edgar_scanner = trading_components["edgar_scanner"]
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

        tickers_iter = dynamic_universe or list(quotes_ingestor.tickers)
        for ticker in tickers_iter:
            try:
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
                                    logger.info(f"🔥 [SCALP DEBUG] 스캘프 신호 발행: {ticker} {quick_signal.signal_type.value} score={quick_signal.score:.3f}")
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
                                    # 스캘프 모드에선 한 틱만 잡으면 충분 — 다음 종목으로
                                    continue
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
                    # LLM 분석 (EDGAR 이벤트이므로 조건 충족)
                    llm_insight = llm_engine.analyze_edgar_filing(edgar_filing)
                
                # 6. 레짐이 vol_spike인 경우 추가 LLM 분석
                if regime_result.regime.value == 'vol_spike' and llm_engine and not llm_insight:
                    # vol_spike 레짐에서 LLM 분석 (조건 충족)
                    text = f"Volatility spike detected for {ticker} in {regime_result.regime.value} regime"
                    llm_insight = llm_engine.analyze_text(text, f"vol_spike_{ticker}", regime='vol_spike')
                
                # 6. 세션별 pre-filter (RTH: 일일상한, EXT: 유동성/스프레드/쿨다운/일일상한)
                ext_enabled = (os.getenv("EXTENDED_PRICE_SIGNALS", "false").lower() in ("1","true","yes","on"))
                session_label = _session_label()
                cutoff_rth, cutoff_ext = get_signal_cutoffs()
                dvol5m = float(indicators.get("dollar_vol_5m", 0.0))
                spread_bp = float(indicators.get("spread_bp", 0.0))
                suppress_reason = None
                
                # RTH 일일 상한 (5건 목표)
                if session_label == "RTH":
                    try:
                        if rurl:
                            r = redis.from_url(rurl)
                            rth_daily_cap = int(os.getenv("RTH_DAILY_CAP", "5"))
                            day_key = f"dailycap:{datetime.utcnow():%Y%m%d}:RTH:{ticker}"
                            used = int(r.get(day_key) or 0)
                            if used >= rth_daily_cap:
                                suppress_reason = "rth_daily_cap"
                    except Exception:
                        pass
                elif session_label == "EXT":
                    if not ext_enabled:
                        suppress_reason = "ext_disabled"
                    if dvol5m < float(os.getenv("EXT_MIN_DOLLAR_VOL_5M", "50000")):
                        suppress_reason = suppress_reason or "low_dvol"
                    if spread_bp > float(os.getenv("EXT_MAX_SPREAD_BP", "300")):
                        suppress_reason = suppress_reason or "wide_spread"
                    # 쿨다운/일일 상한 체크: Redis 키 사용
                    try:
                        if rurl:
                            r = redis.from_url(rurl)
                            cool_min = int(os.getenv("EXT_COOLDOWN_MIN", "7"))
                            daily_cap = 3
                            now_ts = int(time.time())
                            cd_key = f"cooldown:{ticker}"
                            last_ts = int(r.get(cd_key) or 0)
                            if now_ts - last_ts < cool_min * 60:
                                suppress_reason = suppress_reason or "cooldown"
                            day_key = f"dailycap:{datetime.utcnow():%Y%m%d}:{ticker}"
                            used = int(r.get(day_key) or 0)
                            if used >= daily_cap:
                                suppress_reason = suppress_reason or "daily_cap"
                            if not suppress_reason:
                                # 통과 시 다음 카운팅을 위해 임시 마킹(실제 Slack 전송 시 최종 증가 권장)
                                r.setex(cd_key, cool_min*60, now_ts)
                    except Exception:
                        pass

                # VaR95 경량 가드: 최근 리턴 샘플 기반(옵션)
                try:
                    from app.engine.risk import rolling_var95
                    # 샘플은 별도 곳에서 채워진다고 가정; 없으면 스킵
                    rurl = os.getenv("REDIS_URL")
                    var_guard = False
                    if rurl:
                        r = redis.from_url(rurl)
                        key = f"risk:rets:{ticker}:{regime_result.regime.value}"
                        samples = [float(x) for x in (r.lrange(key, 0, 9999) or [])]
                        if samples:
                            var95 = rolling_var95(samples)
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
                        # 억제 사유 상세 로그
                        logger.info(f"억제: reason={suppress_reason or 'below_cutoff'} "
                                   f"score={signal.score:.3f} cut={cut:.3f} dvol5m={dvol5m:.0f} spread_bp={spread_bp:.1f}")
                        
                        # 억제 메트릭 누적
                        try:
                            if rurl:
                                r = redis.from_url(rurl)
                                hkey = f"metrics:suppressed:{datetime.utcnow():%Y%m%d}"
                                r.hincrby(hkey, suppress_reason or "below_cutoff", 1)
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
                    logger.info(f"🔥 [DEBUG] Redis 스트림 발행 시도 중...")
                    
                    try:
                        redis_streams.publish_signal(signal_data)
                        logger.info(f"🔥 [DEBUG] Redis 스트림 발행 성공: {ticker}")
                        
                        # Slack 전송: 강신호만 (원래 기획 - 소수·굵직한 알림)
                        if slack_bot:
                            # 강신호 기준: abs(score) >= cut + 0.20 (더 까다롭게)
                            strong_signal_threshold = cut + 0.20
                            is_strong_signal = abs(signal.score) >= strong_signal_threshold
                            
                            if is_strong_signal:
                                logger.info(f"📢 Slack 전송 (강신호): {ticker} score={signal.score:.3f} >= {strong_signal_threshold:.3f}")
                                try:
                                    slack_message = format_slack_message(signal)
                                    result = slack_bot.send_message(slack_message)
                                    if result:
                                        logger.info(f"✅ Slack 전송 성공: {ticker}")
                                        # 일일 상한 카운터 증가
                                        try:
                                            if rurl:
                                                r = redis.from_url(rurl)
                                                if session_label == "RTH":
                                                    day_key = f"dailycap:{datetime.utcnow():%Y%m%d}:RTH:{ticker}"
                                                    r.incr(day_key)
                                                    r.expire(day_key, 86400)  # 24시간 만료
                                                elif session_label == "EXT":
                                                    day_key = f"dailycap:{datetime.utcnow():%Y%m%d}:EXT:{ticker}"
                                                    r.incr(day_key)
                                                    r.expire(day_key, 86400)  # 24시간 만료
                                        except Exception as e:
                                            logger.warning(f"일일 상한 카운터 업데이트 실패: {e}")
                                    else:
                                        logger.error(f"❌ Slack 전송 실패: {ticker}")
                                except Exception as e:
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
                    
                    logger.info(f"시그널 생성: {ticker} {signal.signal_type.value} (점수: {signal.score:.2f})")
                
            except Exception as e:
                logger.error(f"시그널 생성 실패 ({ticker}): {e}")
                continue
        
        execution_time = time.time() - start_time
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
                    _ind = _d.get("indicators", {}) if isinstance(_d, dict) else {}
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
    if dtime(9,30) <= now <= dtime(16,0):
        return "RTH"
    if dtime(4,0) <= now <= dtime(20,0):
        return "EXT"
    return "CLOSED"

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
                from datetime import date, timedelta
                conn = psycopg2.connect(dsn)
                cur = conn.cursor()
                prev = (datetime.utcnow() - timedelta(days=1)).date()
                cur.execute("SELECT COUNT(*) FROM orders_paper WHERE DATE(ts)=%s", (prev,))
                fills = int(cur.fetchone()[0] or 0)
                cur.close(); conn.close()
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
