"""
레짐 감지 엔진
30초봉/1분봉 혼합으로 Trend/Vol/Mean 분류
"""
import numpy as np
import pandas as pd
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Literal
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)

class RegimeType(Enum):
    """레짐 타입"""
    TREND = "trend"
    VOL_SPIKE = "vol_spike" 
    MEAN_REVERT = "mean_revert"
    SIDEWAYS = "sideways"

@dataclass
class RegimeResult:
    """레짐 감지 결과"""
    regime: RegimeType
    confidence: float  # 0~1
    features: Dict  # 레짐별 특징값들
    timestamp: datetime

class RegimeDetector:
    """레짐 감지기"""
    
    def __init__(self, lookback_minutes: int = 30):
        """
        Args:
            lookback_minutes: 레짐 감지에 사용할 과거 데이터 기간
        """
        self.lookback_minutes = lookback_minutes
        self.min_data_points = 20  # 최소 데이터 포인트
        
        # 레짐별 임계값
        self.thresholds = {
            "trend": {
                "adx_min": 20,  # ADX 최소값
                "ema_ratio_min": 0.005,  # EMA 비율 최소값 (0.5%)
                "price_momentum_min": 0.01  # 가격 모멘텀 최소값 (1%)
            },
            "vol_spike": {
                "volume_ratio_min": 2.0,  # 거래량 비율 최소값 (2배)
                "price_volatility_min": 0.02,  # 가격 변동성 최소값 (2%)
                "volatility_percentile": 95  # 변동성 상위 5%
            },
            "mean_revert": {
                "rsi_extreme_min": 25,  # RSI 극단값 (25 이하 또는 75 이상)
                "bb_position_min": 0.8,  # 볼린저 밴드 위치 (0.8 이상 또는 0.2 이하)
                "reversal_signal_min": 0.3  # 반전 신호 최소값
            }
        }
        
        logger.info(f"레짐 감지기 초기화: {lookback_minutes}분 룩백")
    
    def detect_regime(self, candles: List, indicators: Dict) -> RegimeResult:
        """
        레짐 감지
        
        Args:
            candles: OHLCV 캔들 리스트
            indicators: 기술적 지표 딕셔너리
            
        Returns:
            RegimeResult: 감지된 레짐
        """
        if len(candles) < self.min_data_points:
            return RegimeResult(
                regime=RegimeType.SIDEWAYS,
                confidence=0.0,
                features={},
                timestamp=datetime.now()
            )
        
        # 각 레짐별 점수 계산
        trend_score = self._calculate_trend_score(candles, indicators)
        vol_spike_score = self._calculate_vol_spike_score(candles, indicators)
        mean_revert_score = self._calculate_mean_revert_score(candles, indicators)
        
        # 가장 높은 점수의 레짐 선택
        scores = {
            RegimeType.TREND: trend_score,
            RegimeType.VOL_SPIKE: vol_spike_score,
            RegimeType.MEAN_REVERT: mean_revert_score
        }
        
        best_regime = max(scores, key=scores.get)
        confidence = scores[best_regime]
        
        # 특징값들 수집
        features = {
            "trend_score": trend_score,
            "vol_spike_score": vol_spike_score,
            "mean_revert_score": mean_revert_score,
            "adx": indicators.get("adx", 0),
            "ema_20": indicators.get("ema_20", 0),
            "ema_50": indicators.get("ema_50", 0),
            "rsi": indicators.get("rsi", 50),
            "volume_spike": indicators.get("volume_spike", 0),
            "price_change_5m": indicators.get("price_change_5m", 0),
            "realized_volatility": indicators.get("realized_volatility", 0)
        }
        
        logger.debug(f"레짐 감지: {best_regime.value} (신뢰도: {confidence:.2f})")
        
        return RegimeResult(
            regime=best_regime,
            confidence=confidence,
            features=features,
            timestamp=datetime.now()
        )
    
    def _calculate_trend_score(self, candles: List, indicators: Dict) -> float:
        """추세 레짐 점수 계산 - 20/50EMA↑ & ADX>20"""
        if len(candles) < 10:
            return 0.0
        
        score = 0.0
        weights = 0.0
        
        # 1. ADX (Average Directional Index) - 추세 강도 > 20
        adx = indicators.get("adx", 0)
        if adx > self.thresholds["trend"]["adx_min"]:
            adx_score = min((adx - 20) / 30.0, 1.0)  # 20~50 범위를 0~1로 정규화
            score += adx_score * 0.5
            weights += 0.5
        else:
            return 0.0  # ADX가 20 미만이면 추세 레짐 아님
        
        # 2. EMA 20/50 관계 - 20EMA > 50EMA (상승추세)
        ema_20 = indicators.get("ema_20", 0)
        ema_50 = indicators.get("ema_50", 0)
        
        if ema_20 and ema_50 and ema_50 != 0:
            ema_ratio = (ema_20 - ema_50) / ema_50
            if ema_ratio > self.thresholds["trend"]["ema_ratio_min"]:
                ema_score = min(ema_ratio / 0.02, 1.0)  # 2% 이상이면 1.0
                score += ema_score * 0.5
                weights += 0.5
            else:
                return 0.0  # EMA 조건 불만족
        
        return score / weights if weights > 0 else 0.0
    
    def _calculate_vol_spike_score(self, candles: List, indicators: Dict) -> float:
        """변동성 급등 레짐 점수 계산 - 1분 실현변동성 상위 5%"""
        if len(candles) < 5:
            return 0.0
        
        score = 0.0
        weights = 0.0
        
        # 1. 1분 실현변동성 (상위 5%)
        realized_volatility = indicators.get("realized_volatility", 0)
        if realized_volatility > 0:
            # 상위 5% 기준 (임의로 0.05 이상을 상위 5%로 가정)
            if realized_volatility >= 0.05:
                vol_score = min(realized_volatility / 0.1, 1.0)  # 10% 이상이면 1.0
                score += vol_score * 0.7
                weights += 0.7
            else:
                return 0.0  # 변동성이 상위 5% 미만이면 Vol-Spike 아님
        
        # 2. 거래량 급증 (보조 지표)
        volume_spike = indicators.get("volume_spike", 0)
        if volume_spike > 0:
            vol_score = min(volume_spike, 1.0)  # 이미 0~1로 정규화됨
            score += vol_score * 0.3
            weights += 0.3
        
        return score / weights if weights > 0 else 0.0
    
    def _calculate_mean_revert_score(self, candles: List, indicators: Dict) -> float:
        """평균회귀 레짐 점수 계산 - RSI 14 극단 후 밴드 복귀"""
        if len(candles) < 10:
            return 0.0
        
        score = 0.0
        weights = 0.0
        
        # 1. RSI 14 극단값 (25 이하 또는 75 이상)
        rsi = indicators.get("rsi", 50)
        rsi_extreme = False
        
        if rsi <= 25 or rsi >= 75:
            rsi_extreme = True
            rsi_extreme_score = abs(rsi - 50) / 50  # 0~1로 정규화
            score += rsi_extreme_score * 0.4
            weights += 0.4
        else:
            return 0.0  # RSI가 극단값이 아니면 Mean-Revert 아님
        
        # 2. 볼린저 밴드 복귀 신호
        bb_position = indicators.get("bb_position", 0.5)  # 0~1 (0=하단, 0.5=중앙, 1=상단)
        
        if rsi_extreme:
            # RSI 극단값에서 밴드 중앙으로 복귀하는 신호
            if rsi <= 25:  # 과매도에서 반등
                if bb_position > 0.3:  # 밴드 하단에서 벗어남
                    bb_score = min(bb_position / 0.5, 1.0)
                    score += bb_score * 0.3
                    weights += 0.3
            elif rsi >= 75:  # 과매수에서 하락
                if bb_position < 0.7:  # 밴드 상단에서 벗어남
                    bb_score = min((1 - bb_position) / 0.5, 1.0)
                    score += bb_score * 0.3
                    weights += 0.3
        
        # 3. 반전 신호 (가격 변화 방향 전환)
        price_change_1m = indicators.get("price_change_1m", 0)
        price_change_5m = indicators.get("price_change_5m", 0)
        
        # 단기와 중기 변화 방향이 다르면 반전 신호
        if (price_change_1m * price_change_5m) < 0 and abs(price_change_1m) > 0.005:
            reversal_score = min(abs(price_change_1m) / 0.02, 1.0)
            score += reversal_score * 0.3
            weights += 0.3
        
        return score / weights if weights > 0 else 0.0
    
    def get_regime_weights(self, regime: RegimeType) -> Dict[str, float]:
        """레짐별 시그널 가중치"""
        weights = {
            "trend": {
                "tech": 0.75,
                "sentiment": 0.25
            },
            "vol_spike": {
                "tech": 0.30,
                "sentiment": 0.70
            },
            "mean_revert": {
                "tech": 0.60,
                "sentiment": 0.40
            },
            "sideways": {
                "tech": 0.50,
                "sentiment": 0.50
            }
        }
        
        return weights.get(regime.value, weights["sideways"])
    
    def is_regime_stable(self, recent_regimes: List[RegimeResult], 
                        stability_minutes: int = 5) -> bool:
        """레짐 안정성 확인"""
        if len(recent_regimes) < 3:
            return False
        
        # 최근 N분 내 레짐들
        cutoff_time = datetime.now() - timedelta(minutes=stability_minutes)
        recent = [r for r in recent_regimes if r.timestamp >= cutoff_time]
        
        if len(recent) < 2:
            return False
        
        # 레짐이 일관되게 유지되는지 확인
        regime_types = [r.regime for r in recent]
        most_common = max(set(regime_types), key=regime_types.count)
        
        # 70% 이상이 같은 레짐이면 안정적
        stability_ratio = regime_types.count(most_common) / len(regime_types)
        return stability_ratio >= 0.7
    
    def get_regime_summary(self, recent_regimes: List[RegimeResult]) -> Dict:
        """레짐 요약 정보"""
        if not recent_regimes:
            return {"current_regime": "unknown", "stability": False}
        
        current = recent_regimes[-1]
        
        return {
            "current_regime": current.regime.value,
            "confidence": current.confidence,
            "stability": self.is_regime_stable(recent_regimes),
            "features": current.features,
            "timestamp": current.timestamp.isoformat()
        }
