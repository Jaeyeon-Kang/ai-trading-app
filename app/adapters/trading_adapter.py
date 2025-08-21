"""
트레이딩 어댑터 팩토리
BROKER 설정에 따라 알파카 또는 기존 PaperLedger 선택
GPT-5 권장 리스크 관리 시스템 통합
"""

import os
import logging
from typing import Protocol, List, Optional, Tuple, Any
from dataclasses import dataclass
from datetime import datetime

from app.engine.risk_manager import get_risk_manager

logger = logging.getLogger(__name__)

class TradingProtocol(Protocol):
    """트레이딩 인터페이스 프로토콜"""
    
    def submit_market_order(self, ticker: str, side: str, quantity: int, 
                           signal_id: str = None, meta: dict = None) -> Any:
        """시장가 주문 제출"""
        ...
    
    def get_positions(self) -> List[Any]:
        """현재 포지션 조회"""
        ...
    
    def get_portfolio_summary(self) -> dict:
        """포트폴리오 요약"""
        ...
    
    def get_current_price(self, ticker: str) -> Optional[float]:
        """현재 가격 조회"""
        ...

@dataclass
class UnifiedTrade:
    """통합 거래 객체"""
    trade_id: str
    ticker: str
    side: str
    quantity: int
    price: float
    timestamp: datetime
    signal_id: str = None
    meta: dict = None

@dataclass
class UnifiedPosition:
    """통합 포지션 객체"""
    ticker: str
    quantity: int
    avg_price: float
    current_price: float
    market_value: float
    unrealized_pnl: float
    unrealized_pnl_pct: float

class TradingAdapterFactory:
    """트레이딩 어댑터 팩토리"""
    
    @staticmethod
    def create_adapter() -> TradingProtocol:
        """환경변수에 따라 적절한 어댑터 생성"""
        broker = os.getenv('BROKER', 'paper')
        
        if broker == 'alpaca_paper':
            from app.adapters.alpaca_paper_trading import AlpacaPaperTrading
            logger.info("알파카 페이퍼 트레이딩 어댑터 사용")
            return AlpacaAdapter(AlpacaPaperTrading())
        
        elif broker == 'paper':
            from app.adapters.paper_ledger import PaperLedger
            logger.info("기존 PaperLedger 어댑터 사용")
            return PaperLedgerAdapter(PaperLedger())
        
        else:
            raise ValueError(f"지원하지 않는 브로커: {broker}")

class AlpacaAdapter:
    """알파카 어댑터 (통합 인터페이스) - GPT-5 리스크 관리 통합"""
    
    def __init__(self, alpaca_client):
        self.client = alpaca_client
        self.risk_manager = get_risk_manager()
    
    def submit_market_order(self, ticker: str, side: str, quantity: int = None, 
                           signal_id: str = None, meta: dict = None,
                           entry_price: float = None, stop_loss: float = None,
                           confidence: float = 1.0) -> UnifiedTrade:
        """
        리스크 관리 기반 시장가 주문 제출
        
        Args:
            quantity: 지정시 기존 로직, None시 리스크 기반 계산
            entry_price: 리스크 계산용 진입가 (None시 현재가 사용)
            stop_loss: 손절가 (리스크 계산 필수)
            confidence: 신호 신뢰도 (0-1)
        """
        try:
            # 1. 현재 포트폴리오 정보 가져오기
            portfolio = self.get_portfolio_summary()
            current_positions = self.get_positions()
            
            # 2. 진입가 결정 (제공되지 않으면 현재가 사용)
            if entry_price is None:
                entry_price = self.client.get_current_price(ticker)
                if entry_price is None:
                    raise ValueError(f"현재가를 가져올 수 없음: {ticker}")
            
            # 3. 손절가 기본값 설정 (1.5% 손절)
            if stop_loss is None:
                if side.lower() == 'buy':
                    stop_loss = entry_price * (1 - 0.015)  # 롱 포지션
                else:
                    stop_loss = entry_price * (1 + 0.015)  # 숏 포지션
            
            # 4. 수량이 지정되지 않으면 리스크 기반 계산
            if quantity is None:
                logger.info(f"🎯 {ticker} 리스크 기반 포지션 사이징 시작")
                
                # 신호 데이터 구성
                signal_data = {
                    'ticker': ticker,
                    'entry_price': entry_price,
                    'stop_loss': stop_loss,
                    'confidence': confidence,
                    'side': side
                }
                
                # 현재 포트폴리오 데이터 구성
                portfolio_data = {
                    'equity': portfolio.get('equity', 0),
                    'positions': [
                        {
                            'ticker': pos.ticker,
                            'quantity': pos.quantity,
                            'avg_price': pos.avg_price,
                            'stop_loss': stop_loss,  # 기존 포지션도 동일 손절 가정
                        }
                        for pos in current_positions
                    ],
                    'initial_equity_today': portfolio.get('equity', 0)  # 임시값
                }
                
                # 리스크 체크 및 포지션 크기 계산
                allowed, risk_result = self.risk_manager.should_allow_trade(signal_data, portfolio_data)
                
                if not allowed:
                    error_msg = risk_result.get('error', '알 수 없는 리스크 오류')
                    logger.warning(f"❌ {ticker} 거래 차단: {error_msg}")
                    raise ValueError(f"리스크 관리 차단: {error_msg}")
                
                # 계산된 포지션 크기 사용
                quantity = risk_result.get('position_size', 1)
                logger.info(f"📊 {ticker} 리스크 기반 포지션: {quantity}주 (위험 {risk_result.get('risk_info', {}).get('risk_pct', 0):.2%})")
            
            else:
                logger.info(f"📌 {ticker} 고정 포지션 사용: {quantity}주")
            
            # 5. 실제 주문 실행
            alpaca_trade = self.client.submit_market_order(ticker, side, quantity, signal_id, meta)
            
            # 6. 리스크 정보를 메타데이터에 추가
            enhanced_meta = (meta or {}).copy()
            if quantity is not None and 'risk_info' in locals() and 'risk_result' in locals():
                enhanced_meta.update({
                    'risk_based_sizing': True,
                    'risk_pct': risk_result.get('risk_info', {}).get('risk_pct', 0),
                    'concurrent_risk': risk_result.get('concurrent_risk', 0),
                    'confidence': confidence
                })
            
            return UnifiedTrade(
                trade_id=alpaca_trade.order_id,
                ticker=alpaca_trade.ticker,
                side=alpaca_trade.side,
                quantity=alpaca_trade.quantity,
                price=alpaca_trade.filled_price,
                timestamp=alpaca_trade.filled_at,
                signal_id=alpaca_trade.signal_id,
                meta=enhanced_meta
            )
            
        except Exception as e:
            logger.error(f"❌ {ticker} 리스크 기반 거래 실행 실패: {e}")
            raise
    
    def get_positions(self) -> List[UnifiedPosition]:
        """현재 포지션 조회"""
        alpaca_positions = self.client.get_positions()
        
        unified_positions = []
        for pos in alpaca_positions:
            unified_pos = UnifiedPosition(
                ticker=pos.ticker,
                quantity=pos.quantity,
                avg_price=pos.avg_price,
                current_price=pos.current_price,
                market_value=pos.market_value,
                unrealized_pnl=pos.unrealized_pnl,
                unrealized_pnl_pct=pos.unrealized_pnl_pct
            )
            unified_positions.append(unified_pos)
        
        return unified_positions
    
    def get_portfolio_summary(self) -> dict:
        """포트폴리오 요약"""
        return self.client.get_portfolio_summary()
    
    def get_current_price(self, ticker: str) -> Optional[float]:
        """현재 가격 조회"""
        return self.client.get_current_price(ticker)
    
    def submit_bracket_order(self, ticker: str, side: str, quantity: int,
                           stop_loss_price: float, take_profit_price: float,
                           signal_id: str = None) -> Tuple[UnifiedTrade, str, str]:
        """브래킷 주문 (알파카 전용 기능)"""
        trade, stop_id, profit_id = self.client.submit_bracket_order(
            ticker, side, quantity, stop_loss_price, take_profit_price, signal_id
        )
        
        unified_trade = UnifiedTrade(
            trade_id=trade.order_id,
            ticker=trade.ticker,
            side=trade.side,
            quantity=trade.quantity,
            price=trade.filled_price,
            timestamp=trade.filled_at,
            signal_id=trade.signal_id,
            meta=trade.meta
        )
        
        return unified_trade, stop_id, profit_id

    def submit_eod_exit(self, ticker: str, quantity: float | int, side: str) -> UnifiedTrade:
        """EOD 전용 청산(클로즈/오프닝 예약 포함)"""
        trade = self.client.submit_eod_exit(ticker, quantity, side)
        return UnifiedTrade(
            trade_id=trade.order_id,
            ticker=trade.ticker,
            side=trade.side,
            quantity=trade.quantity,
            price=trade.filled_price,
            timestamp=trade.filled_at,
            signal_id=getattr(trade, 'signal_id', None),
            meta=getattr(trade, 'meta', None)
        )

class PaperLedgerAdapter:
    """기존 PaperLedger 어댑터 (통합 인터페이스)"""
    
    def __init__(self, paper_ledger):
        self.client = paper_ledger
    
    def submit_market_order(self, ticker: str, side: str, quantity: int, 
                           signal_id: str = None, meta: dict = None) -> UnifiedTrade:
        """시장가 주문 제출"""
        # 현재가 가져오기 (임시로 고정값 사용, 실제로는 API 호출)
        price = 100.0  # TODO: 실제 현재가 연동
        
        order_id = f"PAPER_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        paper_trade = self.client.simulate_fill(order_id, ticker, side, quantity, price, meta)
        
        return UnifiedTrade(
            trade_id=paper_trade.trade_id,
            ticker=paper_trade.ticker,
            side=paper_trade.side,
            quantity=paper_trade.quantity,
            price=paper_trade.price,
            timestamp=paper_trade.timestamp,
            signal_id=signal_id,
            meta=paper_trade.meta
        )
    
    def get_positions(self) -> List[UnifiedPosition]:
        """현재 포지션 조회"""
        # PaperLedger에서 포지션 가져오기
        positions = []
        for ticker, pos in self.client.positions.items():
            unified_pos = UnifiedPosition(
                ticker=pos.ticker,
                quantity=pos.quantity,
                avg_price=pos.avg_price,
                current_price=pos.last_price,
                market_value=pos.quantity * pos.last_price,
                unrealized_pnl=pos.unrealized_pnl,
                unrealized_pnl_pct=(pos.unrealized_pnl / (pos.quantity * pos.avg_price)) * 100 if pos.quantity > 0 else 0
            )
            positions.append(unified_pos)
        
        return positions
    
    def get_portfolio_summary(self) -> dict:
        """포트폴리오 요약"""
        daily_stats = self.client.get_daily_stats()
        positions = self.get_positions()
        
        return {
            'cash': daily_stats.get('cash_usd', 0),
            'portfolio_value': daily_stats.get('total_value', 0),
            'equity': daily_stats.get('total_value', 0),
            'buying_power': daily_stats.get('cash_usd', 0),
            'positions_count': len(positions),
            'total_unrealized_pnl': sum(pos.unrealized_pnl for pos in positions),
            'total_market_value': sum(pos.market_value for pos in positions),
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
    
    def get_current_price(self, ticker: str) -> Optional[float]:
        """현재 가격 조회"""
        # TODO: 실제 API 연동
        return 100.0

# 글로벌 어댑터 인스턴스
_trading_adapter = None

def get_trading_adapter() -> TradingProtocol:
    """트레이딩 어댑터 싱글톤"""
    global _trading_adapter
    if _trading_adapter is None:
        _trading_adapter = TradingAdapterFactory.create_adapter()
    return _trading_adapter
