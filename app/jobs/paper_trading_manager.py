"""
스마트 페이퍼 트레이딩 매니저 (Phase 1.5)
자동 포지션 관리, 스톱로스/익절, 성과 추적

기능:
1. 신호 기반 자동 주문 생성
2. 스톱로스/익절 자동 실행  
3. 포지션 사이징 및 리스크 관리
4. 일일/주간 성과 리포트
5. 슬랙 알림 통합
"""

import logging
from datetime import datetime
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass

from app.engine.mixer import TradingSignal
from app.adapters.paper_ledger import PaperLedger, PaperTrade

logger = logging.getLogger(__name__)

@dataclass
class StopLossOrder:
    """스톱로스 주문"""
    ticker: str
    quantity: int
    stop_price: float
    order_id: str
    created_at: datetime
    signal_id: str  # 원본 신호 ID

@dataclass 
class TakeProfitOrder:
    """익절 주문"""
    ticker: str
    quantity: int
    target_price: float
    order_id: str
    created_at: datetime
    signal_id: str  # 원본 신호 ID

@dataclass
class PerformanceMetrics:
    """성과 지표"""
    total_trades: int
    winning_trades: int
    losing_trades: int
    total_pnl: float
    win_rate: float
    avg_win: float
    avg_loss: float
    max_drawdown: float
    sharpe_ratio: float
    timestamp: datetime

class PaperTradingManager:
    """스마트 페이퍼 트레이딩 매니저"""
    
    def __init__(self, initial_cash: float = 1000000.0):
        self.ledger = PaperLedger(initial_cash)
        
        # 리스크 관리 설정
        self.max_position_size_pct = 0.10  # 포트폴리오의 10%까지
        self.max_total_exposure_pct = 0.80  # 총 80%까지 투자
        self.min_cash_reserve_pct = 0.20   # 20% 현금 보유
        
        # 주문 관리
        self.pending_stop_losses: Dict[str, StopLossOrder] = {}
        self.pending_take_profits: Dict[str, TakeProfitOrder] = {}
        
        # 성과 추적
        self.daily_metrics: List[PerformanceMetrics] = []
        self.trade_history: List[Dict] = []
        
        # USD/KRW 환율 (임시)
        self.usd_krw_rate = 1300.0
        
        logger.info(f"📊 스마트 페이퍼 트레이딩 매니저 초기화: {initial_cash:,}원")
    
    def get_trading_components(self):
        """트레이딩 컴포넌트 가져오기"""
        try:
            from app.hooks.autoinit import trading_components
            return trading_components
        except ImportError:
            logger.warning("trading_components를 가져올 수 없음")
            return {}
    
    def should_execute_signal(self, signal: TradingSignal) -> Tuple[bool, str]:
        """신호 실행 여부 판단"""
        
        # 1. 포지션 사이징 체크
        portfolio_value = self.get_portfolio_value()
        portfolio_value * self.max_position_size_pct
        
        # 2. 현금 보유량 체크
        cash_reserve = portfolio_value * self.min_cash_reserve_pct
        available_cash = self.ledger.cash_usd * self.usd_krw_rate
        
        if available_cash < cash_reserve:
            return False, "현금 보유량 부족"
        
        # 3. 총 익스포저 체크
        current_exposure = self.get_total_exposure()
        max_exposure = portfolio_value * self.max_total_exposure_pct
        
        if current_exposure > max_exposure:
            return False, "총 익스포저 한도 초과"
        
        # 4. 동일 종목 중복 포지션 체크
        if signal.ticker in self.ledger.positions:
            existing_pos = self.ledger.positions[signal.ticker]
            
            # 같은 방향 포지션이 이미 있으면 스킵
            if ((signal.signal_type.value == "long" and existing_pos.quantity > 0) or
                (signal.signal_type.value == "short" and existing_pos.quantity < 0)):
                return False, "동일 방향 포지션 존재"
        
        return True, "실행 가능"
    
    def calculate_position_size(self, signal: TradingSignal) -> int:
        """포지션 사이징 계산"""
        portfolio_value = self.get_portfolio_value()
        max_position_value = portfolio_value * self.max_position_size_pct
        
        # 신호 강도에 따른 사이징 조정
        confidence_multiplier = min(1.0, signal.confidence + 0.2)
        adjusted_position_value = max_position_value * confidence_multiplier
        
        # USD로 변환하여 수량 계산
        position_value_usd = adjusted_position_value / self.usd_krw_rate
        quantity = int(position_value_usd / signal.entry_price)
        
        # 최소 1주, 최대 1000주 제한
        return max(1, min(1000, quantity))
    
    def execute_signal(self, signal: TradingSignal) -> Optional[Dict]:
        """신호 실행"""
        
        # 실행 가능성 체크
        can_execute, reason = self.should_execute_signal(signal)
        if not can_execute:
            logger.info(f"신호 실행 스킵: {signal.ticker} - {reason}")
            return None
        
        # 포지션 사이징
        quantity = self.calculate_position_size(signal)
        side = "buy" if signal.signal_type.value == "long" else "sell"
        
        try:
            # 주문 실행
            order_id = f"SIG_{signal.ticker}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            
            trade = self.ledger.simulate_fill(
                order_id=order_id,
                ticker=signal.ticker,
                side=side,
                quantity=quantity,
                price=signal.entry_price,
                meta={
                    "signal_score": signal.score,
                    "signal_confidence": signal.confidence,
                    "signal_regime": signal.regime,
                    "signal_trigger": signal.trigger
                }
            )
            
            # 스톱로스/익절 주문 생성
            self._create_stop_loss_order(signal, trade)
            self._create_take_profit_order(signal, trade)
            
            # 거래 기록
            trade_record = {
                "timestamp": trade.timestamp.isoformat(),
                "ticker": signal.ticker,
                "side": side,
                "quantity": quantity,
                "price": trade.price,
                "signal_score": signal.score,
                "signal_confidence": signal.confidence,
                "portfolio_value": self.get_portfolio_value()
            }
            self.trade_history.append(trade_record)
            
            # 슬랙 알림
            self._send_execution_notification(signal, trade)
            
            logger.info(f"✅ 신호 실행 완료: {signal.ticker} {side} {quantity}주")
            
            return {
                "status": "executed",
                "trade_id": trade.trade_id,
                "ticker": signal.ticker,
                "side": side,
                "quantity": quantity,
                "price": trade.price
            }
            
        except Exception as e:
            logger.error(f"신호 실행 실패: {signal.ticker} - {e}")
            return None
    
    def _create_stop_loss_order(self, signal: TradingSignal, trade: PaperTrade):
        """스톱로스 주문 생성"""
        stop_order = StopLossOrder(
            ticker=signal.ticker,
            quantity=trade.quantity,
            stop_price=signal.stop_loss,
            order_id=f"SL_{trade.trade_id}",
            created_at=datetime.now(),
            signal_id=trade.order_id
        )
        
        self.pending_stop_losses[stop_order.order_id] = stop_order
        logger.info(f"📉 스톱로스 주문 생성: {signal.ticker} @ ${signal.stop_loss:.2f}")
    
    def _create_take_profit_order(self, signal: TradingSignal, trade: PaperTrade):
        """익절 주문 생성"""
        tp_order = TakeProfitOrder(
            ticker=signal.ticker,
            quantity=trade.quantity,
            target_price=signal.take_profit,
            order_id=f"TP_{trade.trade_id}",
            created_at=datetime.now(),
            signal_id=trade.order_id
        )
        
        self.pending_take_profits[tp_order.order_id] = tp_order
        logger.info(f"📈 익절 주문 생성: {signal.ticker} @ ${signal.take_profit:.2f}")
    
    def check_and_execute_stop_orders(self, current_prices: Dict[str, float]):
        """스톱로스/익절 주문 체크 및 실행"""
        executed_orders = []
        
        # 스톱로스 체크
        for order_id, stop_order in list(self.pending_stop_losses.items()):
            current_price = current_prices.get(stop_order.ticker)
            if not current_price:
                continue
                
            # 스톱로스 조건 체크
            if current_price <= stop_order.stop_price:
                try:
                    # 반대 주문 실행
                    side = "sell"  # 손절은 항상 매도
                    
                    trade = self.ledger.simulate_fill(
                        order_id=order_id,
                        ticker=stop_order.ticker,
                        side=side,
                        quantity=stop_order.quantity,
                        price=current_price,
                        meta={"order_type": "stop_loss"}
                    )
                    
                    executed_orders.append(("stop_loss", stop_order, trade))
                    del self.pending_stop_losses[order_id]
                    
                    logger.info(f"🛑 스톱로스 실행: {stop_order.ticker} @ ${current_price:.2f}")
                    
                except Exception as e:
                    logger.error(f"스톱로스 실행 실패: {stop_order.ticker} - {e}")
        
        # 익절 체크
        for order_id, tp_order in list(self.pending_take_profits.items()):
            current_price = current_prices.get(tp_order.ticker)
            if not current_price:
                continue
                
            # 익절 조건 체크
            if current_price >= tp_order.target_price:
                try:
                    # 반대 주문 실행
                    side = "sell"  # 익절도 항상 매도
                    
                    trade = self.ledger.simulate_fill(
                        order_id=order_id,
                        ticker=tp_order.ticker,
                        side=side,
                        quantity=tp_order.quantity,
                        price=current_price,
                        meta={"order_type": "take_profit"}
                    )
                    
                    executed_orders.append(("take_profit", tp_order, trade))
                    del self.pending_take_profits[order_id]
                    
                    logger.info(f"🎯 익절 실행: {tp_order.ticker} @ ${current_price:.2f}")
                    
                except Exception as e:
                    logger.error(f"익절 실행 실패: {tp_order.ticker} - {e}")
        
        # 실행된 주문들에 대한 알림
        for order_type, order, trade in executed_orders:
            self._send_stop_order_notification(order_type, order, trade)
        
        return executed_orders
    
    def get_portfolio_value(self) -> float:
        """포트폴리오 총 가치 (KRW)"""
        # 현재가 정보 가져오기
        current_prices = self._get_current_prices()
        
        # USD 포지션을 KRW로 변환
        usd_value_krw = self.ledger.get_portfolio_value(current_prices) * self.usd_krw_rate
        
        return self.ledger.cash_krw + usd_value_krw
    
    def get_total_exposure(self) -> float:
        """총 익스포저 (KRW)"""
        total_exposure = 0.0
        current_prices = self._get_current_prices()
        
        for ticker, pos in self.ledger.positions.items():
            if ticker in current_prices:
                market_value = abs(pos.quantity) * current_prices[ticker] * self.usd_krw_rate
                total_exposure += market_value
        
        return total_exposure
    
    def _get_current_prices(self) -> Dict[str, float]:
        """현재가 정보 가져오기"""
        try:
            components = self.get_trading_components()
            quotes_ingestor = components.get("quotes_ingestor")
            
            if quotes_ingestor:
                # 모든 포지션의 현재가 조회
                prices = {}
                for ticker in self.ledger.positions.keys():
                    price = quotes_ingestor.get_latest_price(ticker)
                    if price:
                        prices[ticker] = price
                return prices
            
        except Exception as e:
            logger.warning(f"현재가 조회 실패: {e}")
        
        # 폴백: 더미 가격 (실제로는 외부 API 사용)
        return {ticker: 100.0 for ticker in self.ledger.positions.keys()}
    
    def _send_execution_notification(self, signal: TradingSignal, trade: PaperTrade):
        """실행 알림 전송"""
        try:
            components = self.get_trading_components()
            slack_bot = components.get("slack_bot")
            
            if not slack_bot:
                return
            
            side_ko = "매수" if trade.side == "buy" else "매도"
            portfolio_value = self.get_portfolio_value()
            
            message = f"""📊 **페이퍼 트레이딩 실행**

🎯 {trade.ticker} {side_ko} {trade.quantity}주 @ ${trade.price:.2f}
💰 포트폴리오: {portfolio_value:,.0f}원
📈 신호점수: {signal.score:.3f} (신뢰도: {signal.confidence:.2f})
🔧 트리거: {signal.trigger}

SL: ${signal.stop_loss:.2f} | TP: ${signal.take_profit:.2f}"""
            
            slack_bot.send_message({"text": message})
            
        except Exception as e:
            logger.error(f"실행 알림 전송 실패: {e}")
    
    def _send_stop_order_notification(self, order_type: str, order, trade: PaperTrade):
        """스톱 주문 실행 알림"""
        try:
            components = self.get_trading_components()
            slack_bot = components.get("slack_bot")
            
            if not slack_bot:
                return
            
            emoji = "🛑" if order_type == "stop_loss" else "🎯"
            type_ko = "손절" if order_type == "stop_loss" else "익절"
            
            message = f"""{emoji} **{type_ko} 실행**

📍 {trade.ticker} {trade.quantity}주 @ ${trade.price:.2f}
💰 포트폴리오: {self.get_portfolio_value():,.0f}원"""
            
            slack_bot.send_message({"text": message})
            
        except Exception as e:
            logger.error(f"스톱 주문 알림 전송 실패: {e}")
    
    def generate_daily_report(self) -> Dict:
        """일일 성과 리포트 생성"""
        try:
            today = datetime.now().date()
            today_trades = [t for t in self.trade_history 
                          if datetime.fromisoformat(t["timestamp"]).date() == today]
            
            total_trades = len(today_trades)
            winning_trades = sum(1 for t in today_trades if t.get("realized_pnl", 0) > 0)
            losing_trades = total_trades - winning_trades
            
            total_pnl = sum(t.get("realized_pnl", 0) for t in today_trades)
            portfolio_value = self.get_portfolio_value()
            
            win_rate = winning_trades / total_trades if total_trades > 0 else 0.0
            
            report = {
                "date": today.isoformat(),
                "total_trades": total_trades,
                "winning_trades": winning_trades,
                "losing_trades": losing_trades,
                "win_rate": win_rate,
                "total_pnl": total_pnl,
                "portfolio_value": portfolio_value,
                "active_positions": len(self.ledger.positions),
                "pending_stop_losses": len(self.pending_stop_losses),
                "pending_take_profits": len(self.pending_take_profits),
                "timestamp": datetime.now().isoformat()
            }
            
            return report
            
        except Exception as e:
            logger.error(f"일일 리포트 생성 실패: {e}")
            return {}
    
    def reset_daily_stats(self):
        """일일 통계 리셋"""
        self.ledger.daily_pnl = 0.0
        self.ledger.daily_trades = 0
        logger.info("📊 일일 통계 리셋 완료")


# =============================================================================
# Celery Tasks
# =============================================================================

try:
    from app.jobs.scheduler import celery_app
    CELERY_AVAILABLE = True
except ImportError:
    celery_app = None
    CELERY_AVAILABLE = False
    logger.warning("Celery app을 임포트할 수 없음 (테스트 환경일 수 있음)")

# 전역 매니저 인스턴스 (실제로는 의존성 주입 사용)
paper_trading_manager = None

def get_paper_trading_manager():
    """페이퍼 트레이딩 매니저 가져오기"""
    global paper_trading_manager
    if paper_trading_manager is None:
        paper_trading_manager = PaperTradingManager()
    return paper_trading_manager

if CELERY_AVAILABLE and celery_app:
    @celery_app.task(name="app.jobs.paper_trading_manager.check_stop_orders")
    def check_stop_orders():
        """스톱로스/익절 주문 체크 태스크"""
        try:
            manager = get_paper_trading_manager()
            
            # 현재가 정보 가져오기
            current_prices = manager._get_current_prices()
            
            # 스톱 주문 체크 및 실행
            executed_orders = manager.check_and_execute_stop_orders(current_prices)
            
            if executed_orders:
                logger.info(f"✅ 스톱 주문 체크 완료: {len(executed_orders)}건 실행")
                return {"status": "completed", "executed_orders": len(executed_orders)}
            else:
                return {"status": "no_executions"}
                
        except Exception as e:
            logger.error(f"스톱 주문 체크 오류: {e}")
            return {"status": "error", "error": str(e)}
    
    @celery_app.task(name="app.jobs.paper_trading_manager.send_daily_report")
    def send_daily_report():
        """일일 성과 리포트 전송 태스크"""
        try:
            manager = get_paper_trading_manager()
            report = manager.generate_daily_report()
            
            if not report:
                return {"status": "no_report"}
            
            # 슬랙으로 리포트 전송
            components = manager.get_trading_components()
            slack_bot = components.get("slack_bot")
            
            if slack_bot:
                message = f"""📊 **일일 페이퍼 트레이딩 리포트**

📅 날짜: {report['date']}
🎯 거래: {report['total_trades']}건 (승: {report['winning_trades']}, 패: {report['losing_trades']})
📈 승률: {report['win_rate']:.1%}
💰 일일손익: {report['total_pnl']:+.0f}원
💼 포트폴리오: {report['portfolio_value']:,.0f}원
📍 활성포지션: {report['active_positions']}개
🛑 대기주문: SL {report['pending_stop_losses']}개, TP {report['pending_take_profits']}개"""
                
                slack_bot.send_message({"text": message})
                logger.info("✅ 일일 리포트 전송 완료")
                
            return {"status": "sent", "report": report}
            
        except Exception as e:
            logger.error(f"일일 리포트 전송 오류: {e}")
            return {"status": "error", "error": str(e)}

else:
    # Celery가 없을 때 fallback
    def check_stop_orders():
        """Celery 없을 때 fallback"""
        logger.warning("Celery 없이 스톱 주문 체크 호출됨")
        manager = get_paper_trading_manager()
        current_prices = manager._get_current_prices()
        return manager.check_and_execute_stop_orders(current_prices)
    
    def send_daily_report():
        """Celery 없을 때 fallback"""
        logger.warning("Celery 없이 일일 리포트 호출됨")
        manager = get_paper_trading_manager()
        return manager.generate_daily_report()