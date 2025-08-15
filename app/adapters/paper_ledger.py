"""
가상 체결 기록 관리
실제 거래 없이 가상으로 체결을 시뮬레이션하고 기록
"""
from dataclasses import dataclass
from typing import List, Dict
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

@dataclass
class PaperTrade:
    """가상 거래 기록"""
    trade_id: str
    ticker: str
    side: str  # "buy" or "sell"
    quantity: int
    price: float
    timestamp: datetime
    order_id: str
    meta: dict = None

@dataclass
class PaperPosition:
    """가상 포지션"""
    ticker: str
    quantity: int
    avg_price: float
    unrealized_pnl: float
    last_price: float
    timestamp: datetime

class PaperLedger:
    """가상 체결 레저"""
    
    def __init__(self, initial_cash: float = 1000000):
        """
        Args:
            initial_cash: 초기 현금 (원화)
        """
        self.cash_krw = initial_cash
        self.cash_usd = 0.0
        self.positions: Dict[str, PaperPosition] = {}
        self.trades: List[PaperTrade] = []
        self.daily_pnl = 0.0
        self.daily_trades = 0
        
        logger.info(f"페이퍼 레저 초기화: 현금 {initial_cash:,}원")
    
    def simulate_fill(self, order_id: str, ticker: str, side: str, 
                     quantity: int, price: float, meta: dict = None) -> PaperTrade:
        """
        가상 체결 시뮬레이션
        
        Args:
            order_id: 주문 ID
            ticker: 종목 코드
            side: 매수/매도
            quantity: 수량
            price: 체결가
            meta: 메타데이터
            
        Returns:
            PaperTrade: 가상 거래 기록
        """
        # 슬리피지 시뮬레이션 (매수시 +0.1%, 매도시 -0.1%)
        slippage = 0.001 if side == "buy" else -0.001
        fill_price = price * (1 + slippage)
        
        # 거래 기록 생성
        trade = PaperTrade(
            trade_id=f"PAPER_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}",
            ticker=ticker,
            side=side,
            quantity=quantity,
            price=fill_price,
            timestamp=datetime.now(),
            order_id=order_id,
            meta=meta or {}
        )
        
        # 포지션 업데이트
        self._update_position(trade)
        
        # 현금 업데이트
        trade_value = fill_price * quantity
        if side == "buy":
            self.cash_usd -= trade_value
        else:
            self.cash_usd += trade_value
        
        # 거래 기록 저장
        self.trades.append(trade)
        self.daily_trades += 1
        
        logger.info(f"가상 체결: {ticker} {side} {quantity}주 @ ${fill_price:.2f}")
        
        return trade
    
    def _update_position(self, trade: PaperTrade):
        """포지션 업데이트"""
        ticker = trade.ticker
        
        if ticker not in self.positions:
            self.positions[ticker] = PaperPosition(
                ticker=ticker,
                quantity=0,
                avg_price=0.0,
                unrealized_pnl=0.0,
                last_price=trade.price,
                timestamp=trade.timestamp
            )
        
        pos = self.positions[ticker]
        
        if trade.side == "buy":
            # 매수: 평균단가 재계산
            total_cost = pos.quantity * pos.avg_price + trade.quantity * trade.price
            pos.quantity += trade.quantity
            pos.avg_price = total_cost / pos.quantity if pos.quantity > 0 else 0.0
        else:
            # 매도: 수량 차감, 실현손익 계산
            if pos.quantity >= trade.quantity:
                realized_pnl = (trade.price - pos.avg_price) * trade.quantity
                self.daily_pnl += realized_pnl
                pos.quantity -= trade.quantity
                
                if pos.quantity == 0:
                    del self.positions[ticker]
                else:
                    pos.avg_price = pos.avg_price  # 평균단가 유지
        
        pos.last_price = trade.price
        pos.timestamp = trade.timestamp
    
    def get_portfolio_value(self, current_prices: Dict[str, float]) -> float:
        """포트폴리오 총 가치 계산"""
        total_value = self.cash_krw + self.cash_usd
        
        for ticker, pos in self.positions.items():
            if ticker in current_prices:
                market_value = pos.quantity * current_prices[ticker]
                total_value += market_value
                
                # 미실현 손익 업데이트
                pos.unrealized_pnl = market_value - (pos.quantity * pos.avg_price)
        
        return total_value
    
    def get_daily_stats(self) -> dict:
        """일일 통계"""
        return {
            "date": datetime.now().date().isoformat(),
            "trades": self.daily_trades,
            "realized_pnl": self.daily_pnl,
            "cash_krw": self.cash_krw,
            "cash_usd": self.cash_usd,
            "positions": len(self.positions),
            "total_value": self.get_portfolio_value({})  # 현재가 없으면 평균단가 기준
        }
    
    def reset_daily(self):
        """일일 통계 리셋"""
        self.daily_pnl = 0.0
        self.daily_trades = 0
        logger.info("일일 통계 리셋")
    
    def export_trades(self) -> List[dict]:
        """거래 기록 내보내기"""
        return [
            {
                "trade_id": trade.trade_id,
                "ticker": trade.ticker,
                "side": trade.side,
                "quantity": trade.quantity,
                "price": trade.price,
                "timestamp": trade.timestamp.isoformat(),
                "order_id": trade.order_id,
                "meta": trade.meta
            }
            for trade in self.trades
        ]
