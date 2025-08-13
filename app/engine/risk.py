from __future__ import annotations

import numpy as np

def rolling_var95(ret_samples: list[float]) -> float:
    """Compute simple 95% one-tailed VaR on return samples.
    Negative returns expected; returns absolute loss threshold (>0).
    """
    if not ret_samples:
        return 0.0
    arr = np.array(ret_samples, dtype=float)
    # 5th percentile of returns distribution (loss tail)
    q = np.percentile(arr, 5)
    return float(abs(min(q, 0.0)))

def suggest_position_qty(default_qty: int, var95: float, max_risk_r: float = 1.0) -> int:
    """Propose position qty so that expected loss in R does not exceed max_risk_r.
    Here we scale linearly on VaR proxy; for demo keep within 1..3x default.
    """
    if var95 <= 0:
        return default_qty
    scale = max(0.5, min(1.5, max_risk_r / var95))
    qty = int(round(default_qty * scale))
    return max(1, min(3 * default_qty, qty))

"""
리스크 관리 엔진
PnL, VaR95, 셧다운 로직
"""
import numpy as np
import pandas as pd
import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

logger = logging.getLogger(__name__)

class RiskStatus(Enum):
    """리스크 상태"""
    NORMAL = "normal"
    WARNING = "warning"
    CRITICAL = "critical"
    SHUTDOWN = "shutdown"

@dataclass
class RiskMetrics:
    """리스크 지표"""
    daily_pnl: float
    daily_pnl_pct: float
    var_95: float
    max_drawdown: float
    position_count: int
    total_exposure: float
    status: RiskStatus
    timestamp: datetime

class RiskEngine:
    """리스크 관리 엔진"""
    
    def __init__(self, 
                 initial_capital: float = 1000000,
                 daily_loss_limit: float = 0.03,  # 3%
                 var_confidence: float = 0.95,
                 max_positions: int = 5,
                 max_exposure: float = 0.1):  # 10%
        """
        Args:
            initial_capital: 초기 자본
            daily_loss_limit: 일일 손실 한도 (비율)
            var_confidence: VaR 신뢰수준
            max_positions: 최대 포지션 수
            max_exposure: 최대 노출 비율
        """
        self.initial_capital = initial_capital
        self.daily_loss_limit = daily_loss_limit
        self.var_confidence = var_confidence
        self.max_positions = max_positions
        self.max_exposure = max_exposure
        
        # 일일 통계
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self.daily_start = datetime.now().date()
        
        # 포지션 추적
        self.positions: Dict[str, Dict] = {}
        
        # 수익률 히스토리 (VaR 계산용)
        self.returns_history: List[float] = []
        self.max_history_size = 1000
        
        # 리스크 상태
        self.status = RiskStatus.NORMAL
        self.shutdown_reason = ""
        
        logger.info(f"리스크 엔진 초기화: 자본 {initial_capital:,}원, 일일 한도 {daily_loss_limit*100}%")
    
    def update_position(self, ticker: str, side: str, quantity: int, 
                       price: float, timestamp: datetime):
        """포지션 업데이트"""
        if ticker not in self.positions:
            self.positions[ticker] = {
                "quantity": 0,
                "avg_price": 0.0,
                "last_price": price,
                "unrealized_pnl": 0.0,
                "timestamp": timestamp
            }
        
        pos = self.positions[ticker]
        
        if side == "buy":
            # 매수: 평균단가 재계산
            total_cost = pos["quantity"] * pos["avg_price"] + quantity * price
            pos["quantity"] += quantity
            pos["avg_price"] = total_cost / pos["quantity"] if pos["quantity"] > 0 else 0.0
        else:
            # 매도: 수량 차감, 실현손익 계산
            if pos["quantity"] >= quantity:
                realized_pnl = (price - pos["avg_price"]) * quantity
                self.daily_pnl += realized_pnl
                pos["quantity"] -= quantity
                
                if pos["quantity"] == 0:
                    del self.positions[ticker]
                else:
                    pos["avg_price"] = pos["avg_price"]  # 평균단가 유지
        
        pos["last_price"] = price
        pos["timestamp"] = timestamp
        
        # 수익률 히스토리 업데이트
        self._update_returns_history(realized_pnl if side == "sell" else 0.0)
    
    def update_market_prices(self, prices: Dict[str, float]):
        """시장 가격 업데이트 (미실현 손익 계산)"""
        total_unrealized = 0.0
        
        for ticker, pos in self.positions.items():
            if ticker in prices:
                current_price = prices[ticker]
                pos["last_price"] = current_price
                pos["unrealized_pnl"] = (current_price - pos["avg_price"]) * pos["quantity"]
                total_unrealized += pos["unrealized_pnl"]
        
        # 총 손익 = 실현손익 + 미실현손익
        total_pnl = self.daily_pnl + total_unrealized
        
        # 일일 손실 한도 확인
        daily_pnl_pct = total_pnl / self.initial_capital
        
        if daily_pnl_pct <= -self.daily_loss_limit:
            self.status = RiskStatus.SHUTDOWN
            self.shutdown_reason = f"일일 손실 한도 초과: {daily_pnl_pct*100:.2f}%"
            logger.warning(f"리스크 셧다운: {self.shutdown_reason}")
    
    def calculate_var(self, lookback_days: int = 30) -> float:
        """VaR 계산"""
        if len(self.returns_history) < 10:
            return 0.0
        
        # 최근 N일 수익률 사용
        recent_returns = self.returns_history[-min(len(self.returns_history), lookback_days*10):]
        
        if len(recent_returns) < 5:
            return 0.0
        
        # VaR 계산 (히스토리컬 시뮬레이션)
        sorted_returns = sorted(recent_returns)
        var_index = int((1 - self.var_confidence) * len(sorted_returns))
        var_95 = sorted_returns[var_index] if var_index < len(sorted_returns) else sorted_returns[0]
        
        return abs(var_95)  # 절댓값 반환
    
    def check_position_limits(self, new_ticker: str = None) -> Tuple[bool, str]:
        """포지션 한도 확인"""
        # 최대 포지션 수 확인
        if len(self.positions) >= self.max_positions:
            if new_ticker and new_ticker not in self.positions:
                return False, f"최대 포지션 수 초과: {self.max_positions}개"
        
        # 최대 노출 확인
        total_exposure = sum(abs(pos["quantity"] * pos["last_price"]) for pos in self.positions.values())
        exposure_ratio = total_exposure / self.initial_capital
        
        if exposure_ratio > self.max_exposure:
            return False, f"최대 노출 초과: {exposure_ratio*100:.1f}%"
        
        return True, "OK"
    
    def calculate_risk_metrics(self) -> RiskMetrics:
        """리스크 지표 계산"""
        # 총 미실현 손익
        total_unrealized = sum(pos["unrealized_pnl"] for pos in self.positions.values())
        total_pnl = self.daily_pnl + total_unrealized
        daily_pnl_pct = total_pnl / self.initial_capital
        
        # VaR 계산
        var_95 = self.calculate_var()
        
        # 최대 드로다운 (간단한 계산)
        max_drawdown = min(0, daily_pnl_pct)  # 음수일 때만
        
        # 총 노출
        total_exposure = sum(abs(pos["quantity"] * pos["last_price"]) for pos in self.positions.values())
        
        # 리스크 상태 결정
        status = self._determine_risk_status(daily_pnl_pct, var_95)
        
        return RiskMetrics(
            daily_pnl=total_pnl,
            daily_pnl_pct=daily_pnl_pct,
            var_95=var_95,
            max_drawdown=max_drawdown,
            position_count=len(self.positions),
            total_exposure=total_exposure,
            status=status,
            timestamp=datetime.now()
        )
    
    def _determine_risk_status(self, daily_pnl_pct: float, var_95: float) -> RiskStatus:
        """리스크 상태 결정"""
        # 이미 셧다운 상태면 그대로
        if self.status == RiskStatus.SHUTDOWN:
            return RiskStatus.SHUTDOWN
        
        # 일일 손실 한도 80% 도달 시 경고
        if daily_pnl_pct <= -self.daily_loss_limit * 0.8:
            return RiskStatus.WARNING
        
        # VaR가 일일 손실 한도의 50% 초과 시 경고
        if var_95 > self.daily_loss_limit * 0.5:
            return RiskStatus.WARNING
        
        # 일일 손실 한도 초과 시 위험
        if daily_pnl_pct <= -self.daily_loss_limit:
            return RiskStatus.CRITICAL
        
        return RiskStatus.NORMAL
    
    def _update_returns_history(self, realized_pnl: float):
        """수익률 히스토리 업데이트"""
        if realized_pnl != 0:
            # 수익률 계산 (실현손익 / 초기자본)
            return_pct = realized_pnl / self.initial_capital
            self.returns_history.append(return_pct)
            
            # 히스토리 크기 제한
            if len(self.returns_history) > self.max_history_size:
                self.returns_history = self.returns_history[-self.max_history_size:]
    
    def reset_daily(self):
        """일일 통계 리셋"""
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self.daily_start = datetime.now().date()
        self.status = RiskStatus.NORMAL
        self.shutdown_reason = ""
        logger.info("일일 리스크 통계 리셋")
    
    def can_trade(self, ticker: str, side: str, quantity: int, price: float) -> Tuple[bool, str]:
        """거래 가능 여부 확인"""
        # 셧다운 상태 확인
        if self.status == RiskStatus.SHUTDOWN:
            return False, f"리스크 셧다운: {self.shutdown_reason}"
        
        # 포지션 한도 확인
        can_position, position_msg = self.check_position_limits(ticker)
        if not can_position:
            return False, position_msg
        
        # 거래 규모 확인 (단일 거래가 자본의 5% 초과 금지)
        trade_value = quantity * price
        if trade_value > self.initial_capital * 0.05:
            return False, f"거래 규모 초과: {trade_value/self.initial_capital*100:.1f}%"
        
        # VaR 기반 추가 검증
        var_95 = self.calculate_var()
        if var_95 > self.daily_loss_limit * 0.3:  # VaR가 일일 한도의 30% 초과
            return False, f"VaR 한도 초과: {var_95*100:.2f}%"
        
        return True, "OK"
    
    def get_hedge_recommendation(self) -> Optional[Dict]:
        """헷지 권장사항"""
        if self.status != RiskStatus.WARNING:
            return None
        
        metrics = self.calculate_risk_metrics()
        
        # 가장 큰 포지션 찾기
        largest_position = None
        max_exposure = 0.0
        
        for ticker, pos in self.positions.items():
            exposure = abs(pos["quantity"] * pos["last_price"])
            if exposure > max_exposure:
                max_exposure = exposure
                largest_position = ticker
        
        if largest_position:
            return {
                "action": "partial_hedge",
                "ticker": largest_position,
                "reason": f"최대 노출 포지션: {largest_position}",
                "exposure": max_exposure,
                "exposure_pct": max_exposure / self.initial_capital * 100
            }
        
        return None
    
    def get_emergency_actions(self) -> List[Dict]:
        """긴급 조치 목록"""
        actions = []
        
        if self.status == RiskStatus.CRITICAL:
            # 모든 포지션 청산 권장
            for ticker, pos in self.positions.items():
                actions.append({
                    "action": "close_position",
                    "ticker": ticker,
                    "reason": "긴급 리스크 관리",
                    "quantity": pos["quantity"],
                    "side": "sell" if pos["quantity"] > 0 else "buy"
                })
        
        elif self.status == RiskStatus.WARNING:
            # 부분 청산 권장
            hedge_rec = self.get_hedge_recommendation()
            if hedge_rec:
                actions.append(hedge_rec)
        
        return actions
    
    def get_risk_report(self) -> Dict:
        """리스크 리포트"""
        metrics = self.calculate_risk_metrics()
        
        return {
            "status": metrics.status.value,
            "daily_pnl": metrics.daily_pnl,
            "daily_pnl_pct": metrics.daily_pnl_pct * 100,
            "var_95": metrics.var_95 * 100,
            "max_drawdown": metrics.max_drawdown * 100,
            "position_count": metrics.position_count,
            "total_exposure": metrics.total_exposure,
            "exposure_pct": metrics.total_exposure / self.initial_capital * 100,
            "shutdown_reason": self.shutdown_reason,
            "can_trade": self.status != RiskStatus.SHUTDOWN,
            "timestamp": metrics.timestamp.isoformat()
        }
