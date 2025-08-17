"""
위험 기반 포지션 사이징 및 리스크 관리
GPT-5 권장사항 구현
"""

import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime, timedelta
import os

logger = logging.getLogger(__name__)

@dataclass
class RiskConfig:
    """리스크 관리 설정"""
    risk_per_trade: float = 0.005  # 0.5% 위험/트레이드
    max_concurrent_risk: float = 0.02  # 동시위험 총합 2%
    daily_loss_limit: float = 0.02  # 일일 손실 한도 2%
    weekly_loss_limit: float = 0.08  # 주간 손실 한도 8%
    stop_loss_pct: float = 0.015  # 손절선 1.5%
    max_positions: int = 4  # 최대 동시 포지션

@dataclass
class PositionRisk:
    """포지션별 위험 정보"""
    ticker: str
    quantity: int
    entry_price: float
    stop_loss: float
    risk_amount: float  # 달러 금액
    risk_pct: float  # 계좌 대비 %

class RiskManager:
    """리스크 관리자 - GPT-5 권장사항 구현"""
    
    def __init__(self, config: RiskConfig = None):
        self.config = config or RiskConfig()
        self.daily_losses = {}  # {date: loss_amount}
        self.weekly_losses = {}  # {week: loss_amount}
        
        # 환경변수로 설정 오버라이드 가능
        self.config.risk_per_trade = float(os.getenv('RISK_PER_TRADE', self.config.risk_per_trade))
        self.config.max_concurrent_risk = float(os.getenv('MAX_CONCURRENT_RISK', self.config.max_concurrent_risk))
        
        logger.info(f"🛡️ 리스크 관리자 초기화: {self.config.risk_per_trade:.1%}/트레이드, {self.config.max_concurrent_risk:.1%} 동시위험 한도")
    
    def calculate_position_size(self, equity: float, entry_price: float, 
                              stop_loss_price: float, signal_confidence: float = 1.0) -> Tuple[int, dict]:
        """
        위험% 기반 포지션 크기 계산
        
        Args:
            equity: 현재 계좌 자산
            entry_price: 진입가
            stop_loss_price: 손절가
            signal_confidence: 신호 신뢰도 (0-1, 리스크 조정용)
            
        Returns:
            Tuple[포지션 크기, 리스크 정보]
        """
        try:
            # 1. 기본 위험 계산
            base_risk_amount = equity * self.config.risk_per_trade
            
            # 2. 신뢰도 기반 조정
            adjusted_risk = base_risk_amount * signal_confidence
            
            # 3. 실제 손실 금액 계산
            price_diff = abs(entry_price - stop_loss_price)
            if price_diff == 0:
                logger.warning("⚠️ 진입가와 손절가가 동일함")
                return 0, {}
            
            # 4. 포지션 크기 계산
            position_size = int(adjusted_risk / price_diff)
            
            # 5. 최소/최대 제한
            position_size = max(1, position_size)  # 최소 1주
            max_size_by_equity = int(equity * 0.4 / entry_price)  # 최대 40% 노출
            position_size = min(position_size, max_size_by_equity)
            
            # 6. 실제 위험 재계산
            actual_risk_amount = position_size * price_diff
            actual_risk_pct = actual_risk_amount / equity
            
            risk_info = {
                'position_size': position_size,
                'risk_amount': actual_risk_amount,
                'risk_pct': actual_risk_pct,
                'target_risk_pct': self.config.risk_per_trade,
                'confidence_adjustment': signal_confidence,
                'max_loss_usd': actual_risk_amount,
                'exposure_pct': (position_size * entry_price) / equity
            }
            
            logger.info(f"📊 포지션 사이징: {position_size}주 (위험 {actual_risk_pct:.2%}, 노출 {risk_info['exposure_pct']:.1%})")
            
            return position_size, risk_info
            
        except Exception as e:
            logger.error(f"❌ 포지션 사이징 계산 실패: {e}")
            return 1, {}  # 안전한 기본값
    
    def check_concurrent_risk(self, current_positions: List[PositionRisk], 
                            new_risk_pct: float) -> Tuple[bool, str]:
        """
        동시위험 한도 체크 (GPT-5 권장: 2% 캡)
        
        Args:
            current_positions: 현재 포지션들
            new_risk_pct: 신규 포지션 위험%
            
        Returns:
            Tuple[허용 여부, 사유]
        """
        try:
            # 현재 총 위험 계산
            current_total_risk = sum(pos.risk_pct for pos in current_positions)
            new_total_risk = current_total_risk + new_risk_pct
            
            # 한도 체크
            if new_total_risk > self.config.max_concurrent_risk:
                return False, f"동시위험 한도 초과: {new_total_risk:.2%} > {self.config.max_concurrent_risk:.2%}"
            
            # 포지션 수 체크
            if len(current_positions) >= self.config.max_positions:
                return False, f"최대 포지션 수 초과: {len(current_positions)} >= {self.config.max_positions}"
            
            logger.info(f"✅ 동시위험 체크 통과: {new_total_risk:.2%}/{self.config.max_concurrent_risk:.2%}")
            return True, "OK"
            
        except Exception as e:
            logger.error(f"❌ 동시위험 체크 실패: {e}")
            return False, f"체크 오류: {e}"
    
    def check_daily_loss_limit(self, current_equity: float, 
                              initial_equity: float, today: datetime = None) -> Tuple[bool, str]:
        """
        일일 손실 한도 체크 (GPT-5 권장: 2%)
        
        Args:
            current_equity: 현재 자산
            initial_equity: 당일 시작 자산
            today: 날짜 (기본값: 오늘)
            
        Returns:
            Tuple[거래 허용 여부, 사유]
        """
        try:
            if today is None:
                today = datetime.now().date()
            
            # 일일 손실 계산
            daily_loss = initial_equity - current_equity
            daily_loss_pct = daily_loss / initial_equity if initial_equity > 0 else 0
            
            # 한도 체크
            if daily_loss_pct > self.config.daily_loss_limit:
                self.daily_losses[today] = daily_loss
                return False, f"일일 손실 한도 초과: {daily_loss_pct:.2%} > {self.config.daily_loss_limit:.2%}"
            
            # 경고 수준 (한도의 80%)
            warning_level = self.config.daily_loss_limit * 0.8
            if daily_loss_pct > warning_level:
                logger.warning(f"⚠️ 일일 손실 경고: {daily_loss_pct:.2%} (한도: {self.config.daily_loss_limit:.2%})")
            
            return True, f"일일 손실: {daily_loss_pct:.2%}"
            
        except Exception as e:
            logger.error(f"❌ 일일 손실 한도 체크 실패: {e}")
            return False, f"체크 오류: {e}"
    
    def should_allow_trade(self, signal_data: dict, current_portfolio: dict) -> Tuple[bool, dict]:
        """
        종합 리스크 체크 - 거래 허용 여부 판단
        
        Args:
            signal_data: 신호 정보 (ticker, entry_price, stop_loss, confidence 등)
            current_portfolio: 현재 포트폴리오 정보
            
        Returns:
            Tuple[허용 여부, 리스크 정보]
        """
        try:
            equity = current_portfolio.get('equity', 0)
            positions = current_portfolio.get('positions', [])
            
            # 1. 포지션 크기 계산
            entry_price = signal_data.get('entry_price', 0)
            stop_loss = signal_data.get('stop_loss', 0)
            confidence = signal_data.get('confidence', 1.0)
            
            if equity <= 0 or entry_price <= 0 or stop_loss <= 0:
                return False, {'error': '포트폴리오 정보 부족'}
            
            position_size, risk_info = self.calculate_position_size(
                equity, entry_price, stop_loss, confidence
            )
            
            # 2. 현재 포지션 위험 계산
            current_risks = []
            for pos in positions:
                risk_pct = (pos.get('quantity', 0) * abs(pos.get('avg_price', 0) - pos.get('stop_loss', 0))) / equity
                current_risks.append(PositionRisk(
                    ticker=pos.get('ticker', ''),
                    quantity=pos.get('quantity', 0),
                    entry_price=pos.get('avg_price', 0),
                    stop_loss=pos.get('stop_loss', 0),
                    risk_amount=risk_pct * equity,
                    risk_pct=risk_pct
                ))
            
            # 3. 동시위험 체크
            concurrent_ok, concurrent_msg = self.check_concurrent_risk(
                current_risks, risk_info.get('risk_pct', 0)
            )
            
            if not concurrent_ok:
                return False, {'error': concurrent_msg, 'risk_info': risk_info}
            
            # 4. 일일 손실 한도 체크
            initial_equity = current_portfolio.get('initial_equity_today', equity)
            daily_ok, daily_msg = self.check_daily_loss_limit(equity, initial_equity)
            
            if not daily_ok:
                return False, {'error': daily_msg, 'risk_info': risk_info}
            
            # 5. 성공 - 종합 정보 반환
            result = {
                'allowed': True,
                'position_size': position_size,
                'risk_info': risk_info,
                'concurrent_risk': sum(r.risk_pct for r in current_risks) + risk_info.get('risk_pct', 0),
                'daily_status': daily_msg,
                'total_positions': len(current_risks) + 1
            }
            
            logger.info(f"✅ 거래 승인: {signal_data.get('ticker')} {position_size}주 (총위험 {result['concurrent_risk']:.2%})")
            return True, result
            
        except Exception as e:
            logger.error(f"❌ 리스크 체크 실패: {e}")
            return False, {'error': f'리스크 체크 오류: {e}'}

# 글로벌 인스턴스
_risk_manager = None

def get_risk_manager() -> RiskManager:
    """리스크 관리자 싱글톤"""
    global _risk_manager
    if _risk_manager is None:
        _risk_manager = RiskManager()
    return _risk_manager