"""
알파카 페이퍼 트레이딩 어댑터
기존 PaperLedger를 대체하여 실제 알파카 페이퍼 트레이딩 API 사용
"""

import logging
import os
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, StopLossRequest, TakeProfitRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderType
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest
from alpaca.trading.models import Position, Order, TradeAccount

logger = logging.getLogger(__name__)

@dataclass
class AlpacaTrade:
    """알파카 거래 기록"""
    order_id: str
    ticker: str
    side: str
    quantity: int
    filled_price: float
    filled_at: datetime
    signal_id: str = None
    meta: dict = None

@dataclass
class AlpacaPosition:
    """알파카 포지션"""
    ticker: str
    quantity: int
    avg_price: float
    market_value: float
    unrealized_pnl: float
    unrealized_pnl_pct: float
    current_price: float

class AlpacaPaperTrading:
    """알파카 페이퍼 트레이딩 클라이언트"""
    
    def __init__(self):
        """알파카 페이퍼 트레이딩 초기화"""
        # 환경변수에서 API 키 로드
        api_key = os.getenv('ALPACA_API_KEY')
        api_secret = os.getenv('ALPACA_API_SECRET')
        
        if not api_key or not api_secret:
            raise ValueError("ALPACA_API_KEY와 ALPACA_API_SECRET 환경변수가 필요합니다")
        
        # 페이퍼 트레이딩 클라이언트 초기화
        self.trading_client = TradingClient(
            api_key=api_key,
            secret_key=api_secret,
            paper=True  # 페이퍼 트레이딩 모드
        )
        
        # 마켓 데이터 클라이언트 (무료 IEX 데이터 사용)
        self.data_client = StockHistoricalDataClient(
            api_key=api_key,
            secret_key=api_secret
        )
        
        # 계정 정보 로드
        self.account = self.trading_client.get_account()
        logger.info(f"알파카 페이퍼 계정 연결: 잔고 ${float(self.account.cash):,.2f}")
    
    def is_market_open(self) -> bool:
        """시장이 열려있는지 확인"""
        try:
            clock = self.trading_client.get_clock()
            return clock.is_open
        except Exception as e:
            logger.error(f"시장 상태 확인 실패: {e}")
            return False

    def submit_market_order(self, ticker: str, side: str, quantity: int, 
                           signal_id: str = None, meta: dict = None) -> AlpacaTrade:
        """
        시장가 주문 제출
        
        Args:
            ticker: 종목 코드
            side: 'buy' 또는 'sell'
            quantity: 주문 수량
            signal_id: 신호 ID
            meta: 메타데이터
            
        Returns:
            AlpacaTrade: 체결된 거래 정보
        """
        try:
            # 주문 전 시장 상태 확인
            if not self.is_market_open():
                raise Exception("Market is closed")

            # 주문 요청 생성
            order_side = OrderSide.BUY if side.lower() == 'buy' else OrderSide.SELL
            
            market_order_data = MarketOrderRequest(
                symbol=ticker,
                qty=quantity,
                side=order_side,
                time_in_force=TimeInForce.DAY
            )
            
            # 주문 제출
            order = self.trading_client.submit_order(order_data=market_order_data)
            
            # 주문 완료까지 대기 (최대 30초)
            filled_order = self._wait_for_fill(order.id, timeout=30)
            
            if filled_order and filled_order.filled_at:
                trade = AlpacaTrade(
                    order_id=filled_order.id,
                    ticker=ticker,
                    side=side,
                    quantity=int(filled_order.filled_qty),
                    filled_price=float(filled_order.filled_avg_price),
                    filled_at=filled_order.filled_at,
                    signal_id=signal_id,
                    meta=meta or {}
                )
                
                logger.info(f"알파카 체결: {ticker} {side} {quantity}주 @ ${trade.filled_price:.2f}")
                return trade
            else:
                raise Exception(f"주문 체결 실패: {order.id}")
                
        except Exception as e:
            logger.error(f"알파카 주문 실패 {ticker} {side}: {e}")
            raise
    
    def _wait_for_fill(self, order_id: str, timeout: int = 30) -> Optional[Order]:
        """주문 체결 대기"""
        import time
        
        start_time = time.time()
        while time.time() - start_time < timeout:
            order = self.trading_client.get_order_by_id(order_id)
            
            if order.status == 'filled':
                return order
            elif order.status in ['cancelled', 'rejected']:
                logger.warning(f"주문 {order_id} 상태: {order.status}")
                return None
                
            time.sleep(1)  # 1초 대기
        
        logger.warning(f"주문 {order_id} 체결 타임아웃")
        return None
    
    def get_positions(self) -> List[AlpacaPosition]:
        """현재 포지션 조회"""
        try:
            positions = self.trading_client.get_all_positions()
            alpaca_positions = []
            
            for pos in positions:
                if float(pos.qty) != 0:  # 포지션이 있는 종목만
                    alpaca_pos = AlpacaPosition(
                        ticker=pos.symbol,
                        quantity=int(pos.qty),
                        avg_price=float(pos.avg_entry_price),
                        market_value=float(pos.market_value),
                        unrealized_pnl=float(pos.unrealized_pl),
                        unrealized_pnl_pct=float(pos.unrealized_plpc) * 100,
                        current_price=float(pos.current_price)
                    )
                    alpaca_positions.append(alpaca_pos)
            
            return alpaca_positions
            
        except Exception as e:
            logger.error(f"포지션 조회 실패: {e}")
            return []
    
    def get_account_info(self) -> dict:
        """계정 정보 조회"""
        try:
            account = self.trading_client.get_account()
            return {
                'cash': float(account.cash),
                'portfolio_value': float(account.portfolio_value),
                'buying_power': float(account.buying_power),
                'equity': float(account.equity),
                'last_equity': float(account.last_equity),
                'day_trade_count': int(account.daytrade_count)
            }
        except Exception as e:
            logger.error(f"계정 정보 조회 실패: {e}")
            return {}
    
    def get_current_price(self, ticker: str) -> Optional[float]:
        """현재 가격 조회 (IEX 데이터)"""
        try:
            request = StockLatestQuoteRequest(symbol_or_symbols=ticker)
            latest_quote = self.data_client.get_stock_latest_quote(request)
            
            if ticker in latest_quote:
                quote = latest_quote[ticker]
                # bid/ask 중간값 사용
                return (float(quote.bid_price) + float(quote.ask_price)) / 2
            
            return None
            
        except Exception as e:
            logger.error(f"가격 조회 실패 {ticker}: {e}")
            return None
    
    def submit_bracket_order(self, ticker: str, side: str, quantity: int,
                           stop_loss_price: float, take_profit_price: float,
                           signal_id: str = None) -> Tuple[AlpacaTrade, str, str]:
        """
        브래킷 주문 (진입 + 손절 + 익절)
        
        Returns:
            Tuple[AlpacaTrade, stop_order_id, profit_order_id]
        """
        try:
            # 1. 메인 시장가 주문
            main_trade = self.submit_market_order(ticker, side, quantity, signal_id)
            
            # 2. 스톱로스 주문
            stop_side = OrderSide.SELL if side.lower() == 'buy' else OrderSide.BUY
            
            stop_order_data = StopLossRequest(
                symbol=ticker,
                qty=quantity,
                side=stop_side,
                time_in_force=TimeInForce.GTC,
                stop_price=stop_loss_price
            )
            
            stop_order = self.trading_client.submit_order(order_data=stop_order_data)
            
            # 3. 이익실현 주문
            profit_order_data = TakeProfitRequest(
                symbol=ticker,
                qty=quantity,
                side=stop_side,
                time_in_force=TimeInForce.GTC,
                limit_price=take_profit_price
            )
            
            profit_order = self.trading_client.submit_order(order_data=profit_order_data)
            
            logger.info(f"브래킷 주문 완료: {ticker} 진입@${main_trade.filled_price:.2f} "
                       f"손절@${stop_loss_price:.2f} 익절@${take_profit_price:.2f}")
            
            return main_trade, stop_order.id, profit_order.id
            
        except Exception as e:
            logger.error(f"브래킷 주문 실패 {ticker}: {e}")
            raise
    
    def cancel_order(self, order_id: str) -> bool:
        """주문 취소"""
        try:
            self.trading_client.cancel_order_by_id(order_id)
            logger.info(f"주문 취소: {order_id}")
            return True
        except Exception as e:
            logger.error(f"주문 취소 실패 {order_id}: {e}")
            return False
    
    def get_order_history(self, limit: int = 100) -> List[dict]:
        """주문 히스토리 조회"""
        try:
            orders = self.trading_client.get_orders(limit=limit)
            
            history = []
            for order in orders:
                if order.status == 'filled':
                    history.append({
                        'order_id': order.id,
                        'symbol': order.symbol,
                        'side': order.side.value,
                        'quantity': int(order.filled_qty) if order.filled_qty else 0,
                        'filled_price': float(order.filled_avg_price) if order.filled_avg_price else 0,
                        'filled_at': order.filled_at,
                        'status': order.status
                    })
            
            return history
            
        except Exception as e:
            logger.error(f"주문 히스토리 조회 실패: {e}")
            return []
    
    def get_portfolio_summary(self) -> dict:
        """포트폴리오 요약"""
        try:
            account = self.get_account_info()
            positions = self.get_positions()
            
            total_unrealized_pnl = sum(pos.unrealized_pnl for pos in positions)
            total_market_value = sum(pos.market_value for pos in positions)
            
            return {
                'cash': account.get('cash', 0),
                'portfolio_value': account.get('portfolio_value', 0),
                'equity': account.get('equity', 0),
                'buying_power': account.get('buying_power', 0),
                'positions_count': len(positions),
                'total_unrealized_pnl': total_unrealized_pnl,
                'total_market_value': total_market_value,
                'positions': [
                    {
                        'ticker': pos.ticker,
                        'quantity': pos.quantity,
                        'avg_price': pos.avg_price,
                        'current_price': pos.current_price,
                        'market_value': pos.market_value,
                        'unrealized_pnl': pos.unrealized_pnl,
                        'unrealized_pnl_pct': pos.unrealized_pnl_pct
                    }
                    for pos in positions
                ]
            }
            
        except Exception as e:
            logger.error(f"포트폴리오 요약 실패: {e}")
            return {}

# 싱글톤 인스턴스
_alpaca_client = None

def get_alpaca_client() -> AlpacaPaperTrading:
    """알파카 클라이언트 싱글톤"""
    global _alpaca_client
    if _alpaca_client is None:
        _alpaca_client = AlpacaPaperTrading()
    return _alpaca_client