"""
기술적 지표 점수화 엔진
EMA/MACD/RSI/VWAP 편차 → 0~1 점수 변환
"""
import numpy as np
import pandas as pd
import logging
from typing import Dict, List, Optional
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger(__name__)

@dataclass
class TechScore:
    """기술적 점수"""
    overall_score: float  # 0~1 종합 점수
    ema_score: float     # EMA 점수
    macd_score: float    # MACD 점수
    rsi_score: float     # RSI 점수
    vwap_score: float    # VWAP 점수
    timestamp: datetime

class TechScoreEngine:
    """기술적 점수 엔진"""
    
    def __init__(self):
        """초기화"""
        # 점수 계산 파라미터
        self.params = {
            "ema": {
                "weight": 0.3,
                "short_period": 20,
                "long_period": 50
            },
            "macd": {
                "weight": 0.25,
                "fast_period": 12,
                "slow_period": 26,
                "signal_period": 9
            },
            "rsi": {
                "weight": 0.25,
                "period": 14,
                "oversold": 30,
                "overbought": 70
            },
            "vwap": {
                "weight": 0.2,
                "deviation_threshold": 0.02  # 2% 편차
            }
        }
        
        logger.info("기술적 점수 엔진 초기화 완료")
    
    def calculate_tech_score(self, indicators: Dict, candles: List) -> TechScore:
        """
        기술적 점수 계산
        
        Args:
            indicators: 기술적 지표 딕셔너리
            candles: OHLCV 캔들 리스트
            
        Returns:
            TechScore: 기술적 점수
        """
        # 각 지표별 점수 계산
        ema_score = self._calculate_ema_score(indicators)
        macd_score = self._calculate_macd_score(indicators)
        rsi_score = self._calculate_rsi_score(indicators)
        vwap_score = self._calculate_vwap_score(indicators)
        
        # 종합 점수 계산 (상승장일수록 높은 점수)
        overall_score = (
            ema_score * self.params["ema"]["weight"] +
            macd_score * self.params["macd"]["weight"] +
            rsi_score * self.params["rsi"]["weight"] +
            vwap_score * self.params["vwap"]["weight"]
        )
        
        return TechScore(
            overall_score=overall_score,
            ema_score=ema_score,
            macd_score=macd_score,
            rsi_score=rsi_score,
            vwap_score=vwap_score,
            timestamp=datetime.now()
        )
    
    def _calculate_ema_score(self, indicators: Dict) -> float:
        """EMA 점수 계산 - 20/50 EMA 관계"""
        ema_20 = indicators.get("ema_20", 0)
        ema_50 = indicators.get("ema_50", 0)
        
        if not ema_20 or not ema_50 or ema_50 == 0:
            return 0.5  # 중립
        
        # EMA 비율 계산 (20EMA > 50EMA = 상승추세)
        ema_ratio = (ema_20 - ema_50) / ema_50
        
        # -0.05 ~ +0.05 범위를 0~1로 정규화
        # -5% = 0 (강한 하락), 0% = 0.5 (중립), +5% = 1 (강한 상승)
        ema_score = np.clip((ema_ratio + 0.05) / 0.1, 0, 1)
        
        return ema_score
    
    def _calculate_macd_score(self, indicators: Dict) -> float:
        """MACD 점수 계산 - MACD와 신호선 관계"""
        macd = indicators.get("macd", 0)
        macd_signal = indicators.get("macd_signal", 0)
        
        if macd is None or macd_signal is None:
            return 0.5  # 중립
        
        # MACD 히스토그램 (MACD - Signal)
        macd_histogram = macd - macd_signal
        
        # MACD 히스토그램을 0~1로 정규화
        # -2 ~ +2 범위를 0~1로 정규화 (임의 범위)
        macd_score = np.clip((macd_histogram + 2) / 4, 0, 1)
        
        return macd_score
    
    def _calculate_rsi_score(self, indicators: Dict) -> float:
        """RSI 점수 계산 - RSI 14 기준"""
        rsi = indicators.get("rsi", 50)
        
        if rsi is None:
            return 0.5  # 중립
        
        # RSI를 0~1로 정규화 (상승장일수록 높은 점수)
        # 0~100 범위를 0~1로 정규화
        # 0 = 과매도, 50 = 중립, 100 = 과매수
        rsi_score = np.clip(rsi / 100, 0, 1)
        
        return rsi_score
    
    def _calculate_vwap_score(self, indicators: Dict) -> float:
        """VWAP 점수 계산 - 현재가와 VWAP 편차"""
        current_price = indicators.get("current_price", 0)
        vwap = indicators.get("vwap", 0)
        
        if not current_price or not vwap or vwap == 0:
            return 0.5  # 중립
        
        # VWAP 편차 계산
        vwap_deviation = (current_price - vwap) / vwap
        
        # 편차를 0~1로 정규화
        # -0.05 ~ +0.05 범위를 0~1로 정규화
        # -5% = 0 (VWAP 하단), 0% = 0.5 (VWAP), +5% = 1 (VWAP 상단)
        vwap_score = np.clip((vwap_deviation + 0.05) / 0.1, 0, 1)
        
        return vwap_score
    
    def get_signal_direction(self, tech_score: TechScore) -> str:
        """기술적 점수 기반 신호 방향"""
        if tech_score.overall_score >= 0.7:
            return "buy"
        elif tech_score.overall_score <= 0.3:
            return "sell"
        else:
            return "neutral"
    
    def get_signal_strength(self, tech_score: TechScore) -> float:
        """신호 강도 (0~1)"""
        if tech_score.overall_score >= 0.5:
            # 매수 신호 강도
            return (tech_score.overall_score - 0.5) * 2
        else:
            # 매도 신호 강도
            return (0.5 - tech_score.overall_score) * 2
    
    def get_market_regime_score(self, tech_score: TechScore) -> str:
        """시장 레짐 점수"""
        if tech_score.overall_score >= 0.8:
            return "strong_bull"
        elif tech_score.overall_score >= 0.6:
            return "bull"
        elif tech_score.overall_score >= 0.4:
            return "neutral"
        elif tech_score.overall_score >= 0.2:
            return "bear"
        else:
            return "strong_bear"
    
    def get_breakout_score(self, indicators: Dict, candles: List) -> float:
        """돌파 점수 계산 (추세 돌파 감지)"""
        if len(candles) < 10:
            return 0.0
        
        score = 0.0
        
        # 1. 최근 고점 돌파
        recent_high = max(c.high for c in candles[-10:-1])  # 최근 9개 캔들
        current_price = candles[-1].close
        
        if current_price > recent_high:
            breakout_ratio = (current_price - recent_high) / recent_high
            score += min(breakout_ratio * 10, 1.0)  # 10% 돌파 시 1.0
        
        # 2. VWAP 상단 돌파
        vwap = indicators.get("vwap", 0)
        if vwap and current_price > vwap:
            vwap_deviation = indicators.get("vwap_deviation", 0)
            if vwap_deviation > 0:
                score += min(vwap_deviation * 5, 0.5)  # VWAP 10% 상단 시 0.5
        
        # 3. 거래량 동반
        volume_spike = indicators.get("volume_spike", 0)
        if volume_spike > 0.5:  # 거래량 50% 이상 증가
            score += volume_spike * 0.3
        
        return min(score, 1.0)
    
    def get_reversal_score(self, indicators: Dict, candles: List) -> float:
        """반전 점수 계산 (평균회귀 감지)"""
        if len(candles) < 5:
            return 0.0
        
        score = 0.0
        
        # 1. RSI 극단값에서 반전
        rsi = indicators.get("rsi", 50)
        if rsi is not None:
            if rsi <= 30:  # 과매도에서 반등 기대
                score += (30 - rsi) / 30 * 0.5
            elif rsi >= 70:  # 과매수에서 하락 기대
                score += (rsi - 70) / 30 * 0.5
        
        # 2. 단기 모멘텀 반전
        price_change_1m = indicators.get("price_change_1m", 0)
        price_change_5m = indicators.get("price_change_5m", 0)
        
        if price_change_1m and price_change_5m:
            # 단기와 중기 방향이 다르면 반전 신호
            if (price_change_1m * price_change_5m) < 0:
                score += min(abs(price_change_1m) * 10, 0.5)
        
        return min(score, 1.0)
