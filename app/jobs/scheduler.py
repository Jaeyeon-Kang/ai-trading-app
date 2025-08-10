"""
Celery beat 스케줄러
15-30초 주기로 시그널 생성 및 거래 실행
"""
from celery import Celery
from celery.schedules import crontab
import logging
from typing import Dict, List, Optional
from datetime import datetime, timedelta
import os
import time

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
}

@celery_app.task(bind=True, name="app.jobs.scheduler.pipeline_e2e")
def pipeline_e2e(self):
    """E2E 파이프라인: EDGAR 이벤트 → LLM → 레짐 → 믹서 → DB → Slack"""
    try:
        start_time = time.time()
        logger.info("E2E 파이프라인 시작")
        
        # 컴포넌트 확인
        if not all([
            trading_components["stream_consumer"],
            trading_components["llm_engine"],
            trading_components["regime_detector"],
            trading_components["signal_mixer"],
            trading_components["slack_bot"]
        ]):
            logger.warning("일부 컴포넌트가 초기화되지 않음")
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
                regime_result = regime_detector.detect_regime(candles, indicators)
                
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
    from app.engine.techscore import TechScore
    return TechScore(
        overall_score=0.7,
        ema_score=0.8,
        macd_score=0.7,
        rsi_score=0.6,
        vwap_score=0.7,
        timestamp=datetime.now()
    )

def format_slack_message(signal) -> Dict:
    """Slack 메시지 포맷"""
    return {
        "text": f"{signal.ticker} | 레짐 {signal.regime.upper()}({signal.confidence:.2f}) | 점수 {signal.score:+.2f} {'롱' if signal.signal_type.value == 'long' else '숏'}",
        "channel": "#trading-signals"
    }

@celery_app.task(bind=True, name="app.jobs.scheduler.generate_signals")
def generate_signals(self):
    """시그널 생성 작업"""
    try:
        start_time = time.time()
        logger.info("시그널 생성 시작")
        
        # 컴포넌트 확인
        if not all(trading_components.values()):
            logger.warning("일부 컴포넌트가 초기화되지 않음")
            return {"status": "skipped", "reason": "components_not_ready"}
        
        quotes_ingestor = trading_components["quotes_ingestor"]
        edgar_scanner = trading_components["edgar_scanner"]
        regime_detector = trading_components["regime_detector"]
        tech_score_engine = trading_components["tech_score_engine"]
        llm_engine = trading_components["llm_engine"]
        signal_mixer = trading_components["signal_mixer"]
        redis_streams = trading_components["redis_streams"]
        
        signals_generated = 0
        
        # 각 종목별로 시그널 생성
        for ticker in quotes_ingestor.tickers:
            try:
                # 1. 시세 데이터 가져오기
                candles = quotes_ingestor.get_latest_candles(ticker, 50)
                if len(candles) < 20:
                    continue
                
                # 2. 기술적 지표 계산
                indicators = quotes_ingestor.get_technical_indicators(ticker)
                if not indicators:
                    continue
                
                # 3. 레짐 감지
                regime_result = regime_detector.detect_regime(candles, indicators)
                
                # 4. 기술적 점수 계산
                tech_score = tech_score_engine.calculate_tech_score(indicators, candles)
                
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
                
                # 6. 시그널 믹싱
                current_price = candles[-1].close if candles else 0
                signal = signal_mixer.mix_signals(
                    ticker=ticker,
                    regime_result=regime_result,
                    tech_score=tech_score,
                    llm_insight=llm_insight,
                    edgar_filing=edgar_filing,
                    current_price=current_price
                )
                
                if signal:
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
                    
                    redis_streams.publish_signal(signal_data)
                    signals_generated += 1
                    
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
        
        edgar_scanner = trading_components["edgar_scanner"]
        redis_streams = trading_components["redis_streams"]
        llm_engine = trading_components["llm_engine"]
        
        if not edgar_scanner or not redis_streams:
            return {"status": "skipped", "reason": "components_not_ready"}
        
        # EDGAR 공시 스캔
        filings = edgar_scanner.run_scan()
        
        # Redis 스트림에 발행
        for filing in filings:
            redis_streams.publish_edgar(filing)
            
            # LLM 분석 (중요한 공시만)
            if filing.get("impact_score", 0) > 0.7:
                llm_insight = llm_engine.analyze_edgar_filing(filing) if llm_engine else None
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
        logger.debug(f"EDGAR 스캔 완료: {len(filings)}개 공시, {execution_time:.2f}초")
        
        return {
            "status": "success",
            "filings_found": len(filings),
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

def initialize_components(components: Dict):
    """컴포넌트 초기화"""
    global trading_components
    trading_components.update(components)
    logger.info("스케줄러 컴포넌트 초기화 완료")

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

if __name__ == "__main__":
    # 개발용 실행
    celery_app.start()
