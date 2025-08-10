"""
FastAPI 메인 모듈
/health, /signal, /report 등 엔드포인트
"""
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Dict, List, Optional, Any
import logging
import json
from datetime import datetime
import os

# 로깅 설정
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# FastAPI 앱 생성
app = FastAPI(
    title="Trading Bot API",
    description="미국 주식 자동매매 봇 API",
    version="1.0.0"
)

# CORS 설정
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Pydantic 모델들
class HealthResponse(BaseModel):
    status: str
    timestamp: str
    version: str
    uptime: float

class SignalRequest(BaseModel):
    ticker: str
    signal_type: str
    score: float
    confidence: float
    trigger: str
    summary: str
    entry_price: float
    stop_loss: float
    take_profit: float

class SignalResponse(BaseModel):
    signal_id: str
    status: str
    message: str

class ReportRequest(BaseModel):
    date: str
    include_risk: bool = True
    include_llm: bool = True

class ReportResponse(BaseModel):
    date: str
    trades: int
    win_rate: float
    realized_pnl: float
    avg_rr: float
    risk_metrics: Optional[Dict] = None
    llm_usage: Optional[Dict] = None
    timestamp: str

class SystemStatusResponse(BaseModel):
    status: str
    redis_connected: bool
    llm_status: str
    signal_latency_ms: int
    execution_latency_ms: int
    timestamp: str

# 전역 변수 (실제로는 의존성 주입 사용)
trading_bot = None
slack_bot = None
redis_streams = None

@app.on_event("startup")
async def startup_event():
    """앱 시작 시 초기화"""
    global trading_bot, slack_bot, redis_streams
    
    logger.info("Trading Bot API 시작")
    
    # 여기서 실제 컴포넌트들을 초기화
    # trading_bot = TradingBot()
    # slack_bot = SlackBot(token=os.getenv("SLACK_TOKEN"))
    # redis_streams = RedisStreams()

@app.on_event("shutdown")
async def shutdown_event():
    """앱 종료 시 정리"""
    logger.info("Trading Bot API 종료")

@app.get("/", response_model=Dict[str, str])
async def root():
    """루트 엔드포인트"""
    return {
        "message": "Trading Bot API",
        "version": "1.0.0",
        "status": "running"
    }

@app.get("/health", response_model=HealthResponse)
async def health_check():
    """헬스 체크"""
    try:
        # 시스템 상태 확인
        redis_health = {"status": "unknown"}
        llm_health = {"status": "unknown"}
        
        if redis_streams:
            redis_health = redis_streams.health_check()
        
        # 전체 상태 결정
        if redis_health.get("status") == "healthy":
            status = "healthy"
        else:
            status = "degraded"
        
        return HealthResponse(
            status=status,
            timestamp=datetime.now().isoformat(),
            version="1.0.0",
            uptime=0.0  # 실제로는 시작 시간부터 계산
        )
        
    except Exception as e:
        logger.error(f"헬스 체크 실패: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/status", response_model=SystemStatusResponse)
async def system_status():
    """시스템 상태 상세"""
    try:
        # Redis 상태
        redis_connected = False
        if redis_streams:
            health = redis_streams.health_check()
            redis_connected = health.get("redis_connected", False)
        
        # LLM 상태
        llm_status = "unknown"
        # if trading_bot and trading_bot.llm_engine:
        #     llm_status = trading_bot.llm_engine.get_status()["status"]
        
        # 지연 시간 (실제로는 측정)
        signal_latency = 150  # ms
        execution_latency = 300  # ms
        
        return SystemStatusResponse(
            status="healthy" if redis_connected else "degraded",
            redis_connected=redis_connected,
            llm_status=llm_status,
            signal_latency_ms=signal_latency,
            execution_latency_ms=execution_latency,
            timestamp=datetime.now().isoformat()
        )
        
    except Exception as e:
        logger.error(f"시스템 상태 조회 실패: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/signal", response_model=SignalResponse)
async def submit_signal(signal: SignalRequest, background_tasks: BackgroundTasks):
    """거래 시그널 제출"""
    try:
        # 시그널 검증
        if signal.score < -1 or signal.score > 1:
            raise HTTPException(status_code=400, detail="점수는 -1에서 1 사이여야 합니다")
        
        if signal.confidence < 0 or signal.confidence > 1:
            raise HTTPException(status_code=400, detail="신뢰도는 0에서 1 사이여야 합니다")
        
        # 시그널 ID 생성
        signal_id = f"signal_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
        
        # 백그라운드에서 처리
        background_tasks.add_task(process_signal, signal_id, signal)
        
        logger.info(f"시그널 제출: {signal_id} - {signal.ticker} {signal.signal_type}")
        
        return SignalResponse(
            signal_id=signal_id,
            status="submitted",
            message="시그널이 제출되었습니다"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"시그널 제출 실패: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/signals", response_model=List[Dict])
async def get_signals(limit: int = 10, ticker: Optional[str] = None):
    """최근 시그널 조회"""
    try:
        # 실제로는 데이터베이스에서 조회
        signals = [
            {
                "signal_id": "signal_20250101_120000_123456",
                "ticker": "AAPL",
                "signal_type": "buy",
                "score": 0.75,
                "confidence": 0.8,
                "timestamp": datetime.now().isoformat(),
                "status": "processed"
            }
        ]
        
        # 필터링
        if ticker:
            signals = [s for s in signals if s["ticker"] == ticker]
        
        # 제한
        signals = signals[:limit]
        
        return signals
        
    except Exception as e:
        logger.error(f"시그널 조회 실패: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/report", response_model=ReportResponse)
async def generate_report(request: ReportRequest):
    """일일 리포트 생성"""
    try:
        # 실제로는 데이터베이스에서 조회
        report_data = {
            "date": request.date,
            "trades": 5,
            "win_rate": 0.6,
            "realized_pnl": 1500.0,
            "avg_rr": 1.4
        }
        
        # 리스크 지표 포함
        if request.include_risk:
            report_data["risk_metrics"] = {
                "var_95": 0.02,
                "max_drawdown": -0.05,
                "position_count": 2,
                "exposure_pct": 0.08
            }
        
        # LLM 사용량 포함
        if request.include_llm:
            report_data["llm_usage"] = {
                "daily_calls": 25,
                "monthly_cost_usd": 12.50
            }
        
        return ReportResponse(
            date=report_data["date"],
            trades=report_data["trades"],
            win_rate=report_data["win_rate"],
            realized_pnl=report_data["realized_pnl"],
            avg_rr=report_data["avg_rr"],
            risk_metrics=report_data.get("risk_metrics"),
            llm_usage=report_data.get("llm_usage"),
            timestamp=datetime.now().isoformat()
        )
        
    except Exception as e:
        logger.error(f"리포트 생성 실패: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/positions", response_model=List[Dict])
async def get_positions():
    """현재 포지션 조회"""
    try:
        # 실제로는 포지션 데이터에서 조회
        positions = [
            {
                "ticker": "AAPL",
                "quantity": 10,
                "avg_price": 150.25,
                "current_price": 152.30,
                "unrealized_pnl": 20.50,
                "timestamp": datetime.now().isoformat()
            }
        ]
        
        return positions
        
    except Exception as e:
        logger.error(f"포지션 조회 실패: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/trades", response_model=List[Dict])
async def get_trades(limit: int = 20, ticker: Optional[str] = None):
    """최근 거래 조회"""
    try:
        # 실제로는 거래 데이터에서 조회
        trades = [
            {
                "trade_id": "trade_20250101_120000_123456",
                "ticker": "AAPL",
                "side": "buy",
                "quantity": 10,
                "price": 150.25,
                "timestamp": datetime.now().isoformat(),
                "status": "filled"
            }
        ]
        
        # 필터링
        if ticker:
            trades = [t for t in trades if t["ticker"] == ticker]
        
        # 제한
        trades = trades[:limit]
        
        return trades
        
    except Exception as e:
        logger.error(f"거래 조회 실패: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/emergency-stop")
async def emergency_stop():
    """긴급 중지"""
    try:
        # 실제로는 거래 중지 로직 실행
        logger.warning("긴급 중지 요청됨")
        
        # Slack 알림 전송
        if slack_bot:
            slack_bot.send_message({
                "text": "🛑 긴급 중지가 요청되었습니다",
                "channel": "#trading-signals"
            })
        
        return {
            "status": "stopped",
            "message": "긴급 중지가 실행되었습니다",
            "timestamp": datetime.now().isoformat()
        }
        
    except Exception as e:
        logger.error(f"긴급 중지 실패: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/config")
async def get_config():
    """설정 조회"""
    try:
        config = {
            "broker": os.getenv("BROKER", "kis"),
            "auto_mode": os.getenv("AUTO_MODE", "0") == "1",
            "llm_monthly_cap_krw": float(os.getenv("LLM_MONTHLY_CAP_KRW", "80000")),
            "daily_loss_limit": 0.03,
            "max_positions": 5,
            "max_exposure": 0.1
        }
        
        return config
        
    except Exception as e:
        logger.error(f"설정 조회 실패: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/config")
async def update_config(config: Dict[str, Any]):
    """설정 업데이트"""
    try:
        # 설정 검증
        if "daily_loss_limit" in config:
            if config["daily_loss_limit"] < 0 or config["daily_loss_limit"] > 0.1:
                raise HTTPException(status_code=400, detail="일일 손실 한도는 0-10% 사이여야 합니다")
        
        if "max_positions" in config:
            if config["max_positions"] < 1 or config["max_positions"] > 10:
                raise HTTPException(status_code=400, detail="최대 포지션 수는 1-10개 사이여야 합니다")
        
        # 실제로는 설정 저장
        logger.info(f"설정 업데이트: {config}")
        
        return {
            "status": "updated",
            "message": "설정이 업데이트되었습니다",
            "timestamp": datetime.now().isoformat()
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"설정 업데이트 실패: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# 백그라운드 작업
async def process_signal(signal_id: str, signal: SignalRequest):
    """시그널 처리 (백그라운드)"""
    try:
        logger.info(f"시그널 처리 시작: {signal_id}")
        
        # 1. 리스크 검증
        # if trading_bot and trading_bot.risk_engine:
        #     can_trade, reason = trading_bot.risk_engine.can_trade(
        #         signal.ticker, signal.signal_type, 1, signal.entry_price
        #     )
        #     if not can_trade:
        #         logger.warning(f"리스크 검증 실패: {reason}")
        #         return
        
        # 2. Slack 알림 전송
        if slack_bot:
            notification = {
                "ticker": signal.ticker,
                "signal_type": signal.signal_type,
                "score": signal.score,
                "confidence": signal.confidence,
                "trigger": signal.trigger,
                "summary": signal.summary,
                "entry_price": signal.entry_price,
                "stop_loss": signal.stop_loss,
                "take_profit": signal.take_profit,
                "timestamp": datetime.now()
            }
            slack_bot.send_signal_notification(notification)
        
        # 3. Redis 스트림에 발행
        if redis_streams:
            signal_data = {
                "signal_id": signal_id,
                "ticker": signal.ticker,
                "signal_type": signal.signal_type,
                "score": signal.score,
                "confidence": signal.confidence,
                "trigger": signal.trigger,
                "summary": signal.summary,
                "entry_price": signal.entry_price,
                "stop_loss": signal.stop_loss,
                "take_profit": signal.take_profit
            }
            redis_streams.publish_signal(signal_data)
        
        logger.info(f"시그널 처리 완료: {signal_id}")
        
    except Exception as e:
        logger.error(f"시그널 처리 실패 ({signal_id}): {e}")

# 에러 핸들러
@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    """전역 예외 처리"""
    logger.error(f"예외 발생: {exc}")
    return {
        "error": "Internal server error",
        "message": str(exc),
        "timestamp": datetime.now().isoformat()
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
