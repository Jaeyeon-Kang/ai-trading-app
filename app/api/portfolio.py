"""
포트폴리오 API 엔드포인트 (알파카 연동)
GPT-5 리스크 관리 시스템 통합
"""

from fastapi import APIRouter, HTTPException
import logging
from typing import Dict, List

from app.adapters.trading_adapter import get_trading_adapter
from app.engine.risk_manager import get_risk_manager

logger = logging.getLogger(__name__)
router = APIRouter()

@router.get("/portfolio/summary")
async def get_portfolio_summary():
    """포트폴리오 요약 정보 (리스크 지표 포함)"""
    try:
        trading_adapter = get_trading_adapter()
        risk_manager = get_risk_manager()
        
        # 기본 포트폴리오 정보
        summary = trading_adapter.get_portfolio_summary()
        positions = trading_adapter.get_positions()
        
        # 리스크 지표 계산
        equity = summary.get('equity', 0)
        
        # 현재 포지션들의 위험 계산
        position_risks = []
        total_risk_pct = 0
        
        for pos in positions:
            # 각 포지션의 리스크 계산 (1.5% 손절 가정)
            stop_loss = pos.avg_price * 0.985 if pos.quantity > 0 else pos.avg_price * 1.015
            risk_amount = abs(pos.quantity * (pos.avg_price - stop_loss))
            risk_pct = risk_amount / equity if equity > 0 else 0
            
            position_risks.append({
                'ticker': pos.ticker,
                'risk_amount': risk_amount,
                'risk_pct': risk_pct,
                'exposure_pct': (pos.quantity * pos.current_price) / equity if equity > 0 else 0
            })
            total_risk_pct += risk_pct
        
        # 리스크 상태 판정
        risk_status = "safe"
        if total_risk_pct > risk_manager.config.max_concurrent_risk * 0.8:
            risk_status = "warning"
        if total_risk_pct > risk_manager.config.max_concurrent_risk:
            risk_status = "exceeded"
        
        # 추가 거래 여력 계산
        remaining_risk_capacity = max(0, risk_manager.config.max_concurrent_risk - total_risk_pct)
        max_new_position_value = remaining_risk_capacity * equity / 0.015  # 1.5% 손절 기준
        
        # 확장된 데이터 구성
        enhanced_summary = {
            **summary,
            "risk_metrics": {
                "current_risk_pct": total_risk_pct,
                "max_risk_pct": risk_manager.config.max_concurrent_risk,
                "risk_status": risk_status,
                "remaining_capacity": remaining_risk_capacity,
                "max_new_position_value": max_new_position_value,
                "daily_loss_limit": risk_manager.config.daily_loss_limit,
                "position_risks": position_risks
            },
            "config": {
                "risk_per_trade": risk_manager.config.risk_per_trade,
                "max_positions": risk_manager.config.max_positions,
                "stop_loss_pct": risk_manager.config.stop_loss_pct
            }
        }
        
        return {
            "status": "success",
            "data": enhanced_summary
        }
        
    except Exception as e:
        logger.error(f"포트폴리오 요약 조회 실패: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/portfolio/positions")
async def get_positions():
    """현재 포지션 목록"""
    try:
        trading_adapter = get_trading_adapter()
        positions = trading_adapter.get_positions()
        
        return {
            "status": "success",
            "data": [
                {
                    "ticker": pos.ticker,
                    "quantity": pos.quantity,
                    "avg_price": pos.avg_price,
                    "current_price": pos.current_price,
                    "market_value": pos.market_value,
                    "unrealized_pnl": pos.unrealized_pnl,
                    "unrealized_pnl_pct": pos.unrealized_pnl_pct
                }
                for pos in positions
            ]
        }
        
    except Exception as e:
        logger.error(f"포지션 조회 실패: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/portfolio/price/{ticker}")
async def get_current_price(ticker: str):
    """종목 현재가 조회"""
    try:
        trading_adapter = get_trading_adapter()
        price = trading_adapter.get_current_price(ticker.upper())
        
        if price is None:
            raise HTTPException(status_code=404, detail=f"가격 정보를 찾을 수 없습니다: {ticker}")
        
        return {
            "status": "success",
            "data": {
                "ticker": ticker.upper(),
                "price": price,
                "timestamp": "real-time"
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"가격 조회 실패 {ticker}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/portfolio/order")
async def submit_order(order_data: dict):
    """리스크 관리 기반 주문 제출"""
    try:
        trading_adapter = get_trading_adapter()
        
        # 필수 필드 검증
        required_fields = ["ticker", "side"]
        for field in required_fields:
            if field not in order_data:
                raise HTTPException(status_code=400, detail=f"필수 필드 누락: {field}")
        
        # 주문 실행 (리스크 기반 사이징 또는 수동 지정)
        trade = trading_adapter.submit_market_order(
            ticker=order_data["ticker"].upper(),
            side=order_data["side"].lower(),
            quantity=order_data.get("quantity"),  # None이면 리스크 기반 계산
            signal_id=order_data.get("signal_id"),
            meta=order_data.get("meta", {}),
            entry_price=order_data.get("entry_price"),
            stop_loss=order_data.get("stop_loss"),
            confidence=order_data.get("confidence", 1.0)
        )
        
        return {
            "status": "success",
            "data": {
                "trade_id": trade.trade_id,
                "ticker": trade.ticker,
                "side": trade.side,
                "quantity": trade.quantity,
                "price": trade.price,
                "timestamp": trade.timestamp.isoformat()
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"주문 실행 실패: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/portfolio/performance")
async def get_performance_metrics():
    """성과 분석 (기본적인 지표)"""
    try:
        trading_adapter = get_trading_adapter()
        
        # 포트폴리오 요약에서 기본 정보 추출
        summary = trading_adapter.get_portfolio_summary()
        positions = trading_adapter.get_positions()
        
        # 간단한 성과 지표 계산
        total_unrealized_pnl = sum(pos.unrealized_pnl for pos in positions)
        total_market_value = sum(pos.market_value for pos in positions)
        
        winning_positions = [pos for pos in positions if pos.unrealized_pnl > 0]
        losing_positions = [pos for pos in positions if pos.unrealized_pnl < 0]
        
        win_rate = (len(winning_positions) / len(positions) * 100) if positions else 0
        
        return {
            "status": "success",
            "data": {
                "portfolio_value": summary.get("portfolio_value", 0),
                "cash": summary.get("cash", 0),
                "total_unrealized_pnl": total_unrealized_pnl,
                "total_market_value": total_market_value,
                "positions_count": len(positions),
                "winning_positions": len(winning_positions),
                "losing_positions": len(losing_positions),
                "win_rate": round(win_rate, 2),
                "best_performer": max(positions, key=lambda x: x.unrealized_pnl_pct).ticker if positions else None,
                "worst_performer": min(positions, key=lambda x: x.unrealized_pnl_pct).ticker if positions else None
            }
        }
        
    except Exception as e:
        logger.error(f"성과 분석 실패: {e}")
        raise HTTPException(status_code=500, detail=str(e))