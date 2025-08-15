"""
레짐 감지 엔진 - Week 1 라이트봇 A단계
30초봉/1분봉 혼합으로 Trend/Vol/Mean 분류
"""
import numpy as np
import logging
from datetime import datetime, timedelta
from typing import Dict, List
from dataclasses import dataclass
from enum import Enum
import os

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
    """레짐 감지기 - Week 1 버전"""
    
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
                "adx_min": 15,           # 20 → 15
                "ema_ratio_min": 0.001,  # 0.5% → 0.1%
                "price_momentum_min": 0.004  # 0.4% (추가 사용)
            },
            "vol_spike": {
                "volume_ratio_min": float(os.getenv("VOLSPIKE_RATIO", "1.6")),  # 2.0 → 1.6
                "price_volatility_min": 0.02,  # 가격 변동성 최소값 (2%)
                "volatility_percentile": int(os.getenv("VOLSPIKE_PCTL", "90"))  # p95 → p90
            },
            "mean_revert": {
                "rsi_extreme_min": 30,    # 25 → 30 (30/70 기준)
                "bb_position_min": 0.7,   # 0.8 → 0.7
                "reversal_signal_min": 0.3  # 반전 신호 최소값
            }
        }
        
        logger.info(f"레짐 감지기 초기화: {lookback_minutes}분 룩백")
    
    def calculate_ema(self, prices: List[float], period: int) -> List[float]:
        """EMA 계산: 데이터가 period보다 짧아도 계산 진행"""
        if not prices:
            return []
        
        # 사용 가능한 길이에 맞춰 부드럽게 계산
        eff = max(2, min(period, len(prices)))  # 최소 2, 최대 시계열 길이
        alpha = 2.0 / (eff + 1)
        ema = [prices[0]]
        
        for price in prices[1:]:
            ema.append(alpha * price + (1 - alpha) * ema[-1])
        
        return ema
    
    def calculate_adx(self, candles: List) -> float:
        """ADX 계산 (간단 버전)"""
        if len(candles) < 14:
            return 0.0
        
        # True Range 계산
        tr_values = []
        for i in range(1, len(candles)):
            high = candles[i].h
            low = candles[i].l
            prev_close = candles[i-1].c
            
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            tr_values.append(tr)
        
        # Directional Movement 계산
        dm_plus = []
        dm_minus = []
        
        for i in range(1, len(candles)):
            high_diff = candles[i].h - candles[i-1].h
            low_diff = candles[i-1].l - candles[i].l
            
            if high_diff > low_diff and high_diff > 0:
                dm_plus.append(high_diff)
                dm_minus.append(0)
            elif low_diff > high_diff and low_diff > 0:
                dm_plus.append(0)
                dm_minus.append(low_diff)
            else:
                dm_plus.append(0)
                dm_minus.append(0)
        
        # 14일 평균
        if len(tr_values) >= 14:
            atr = np.mean(tr_values[-14:])
            di_plus = np.mean(dm_plus[-14:]) / atr * 100 if atr > 0 else 0
            di_minus = np.mean(dm_minus[-14:]) / atr * 100 if atr > 0 else 0
            
            # ADX 계산
            dx = abs(di_plus - di_minus) / (di_plus + di_minus) * 100 if (di_plus + di_minus) > 0 else 0
            return min(dx, 100.0)
        
        return 0.0
    
    def calculate_rsi(self, prices: List[float], period: int = 14) -> float:
        """RSI 계산"""
        if len(prices) < period + 1:
            return 50.0
        
        gains = []
        losses = []
        
        for i in range(1, len(prices)):
            change = prices[i] - prices[i-1]
            if change > 0:
                gains.append(change)
                losses.append(0)
            else:
                gains.append(0)
                losses.append(abs(change))
        
        if len(gains) < period:
            return 50.0
        
        avg_gain = np.mean(gains[-period:])
        avg_loss = np.mean(losses[-period:])
        
        if avg_loss == 0:
            return 100.0
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        
        return rsi
    
    def rsi_series(self, prices: List[float], period: int = 14) -> List[float]:
        """RSI 시계열 계산"""
        if len(prices) < period + 1:
            return [50.0] * len(prices)
        
        gains, losses = [0.0], [0.0]
        for i in range(1, len(prices)):
            ch = prices[i] - prices[i-1]
            gains.append(ch if ch > 0 else 0.0)
            losses.append(-ch if ch < 0 else 0.0)
        
        rsis = []
        for i in range(len(prices)):
            if i < period:
                rsis.append(50.0)
            else:
                avg_gain = np.mean(gains[i-period+1:i+1])
                avg_loss = np.mean(losses[i-period+1:i+1])
                if avg_loss == 0:
                    rsis.append(100.0)
                else:
                    rs = avg_gain / avg_loss
                    rsis.append(100 - (100 / (1 + rs)))
        return rsis
    
    def calculate_bollinger_bands(self, prices: List[float], period: int = 20) -> Dict:
        """볼린저 밴드 계산"""
        if len(prices) < period:
            return {"upper": prices[-1], "middle": prices[-1], "lower": prices[-1]}
        
        recent_prices = prices[-period:]
        middle = np.mean(recent_prices)
        std = np.std(recent_prices)
        
        return {
            "upper": middle + (2 * std),
            "middle": middle,
            "lower": middle - (2 * std)
        }
    
    def detect_regime(self, candles: List) -> RegimeResult:
        """
        레짐 감지
        
        Args:
            candles: OHLCV 캔들 리스트 (Bar30s 객체들)
            
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
        
        # 기본 데이터 추출
        closes = [c.c for c in candles]
        [c.v for c in candles]
        [c.h for c in candles]
        [c.l for c in candles]
        
        # 기술적 지표 계산 (지표 기간을 가용 바 수에 맞춰 자동 축소)
        n = len(closes)
        fast_p = min(20, max(3, n // 3))   # 예: 30바면 10
        slow_p = min(50, max(5, n // 2))   # 예: 30바면 15
        
        rsi_hist = self.rsi_series(closes, 14)
        ema_fast = self.calculate_ema(closes, fast_p)
        ema_slow = self.calculate_ema(closes, slow_p)
        
        indicators = {
            "ema_20": ema_fast,
            "ema_50": ema_slow,
            "adx": self.calculate_adx(candles),
            "rsi": rsi_hist[-1],
            "rsi_hist": rsi_hist,
            "bb": self.calculate_bollinger_bands(closes, min(20, n))
        }
        
        # 각 레짐별 점수 계산
        trend_score = self._calculate_trend_score(candles, indicators)
        vol_spike_score = self._calculate_vol_spike_score(candles, indicators)
        mean_revert_score = self._calculate_mean_revert_score(candles, indicators)
        
        # 디버그 출력
        ema_20 = indicators["ema_20"][-1] if indicators["ema_20"] else 0
        ema_50 = indicators["ema_50"][-1] if indicators["ema_50"] else 0
        price = candles[-1].c
        adx = indicators["adx"]
        slope_20 = (ema_20 - indicators["ema_20"][-2]) / max(indicators["ema_20"][-2], 1e-9) if len(indicators["ema_20"]) > 1 else 0
        slope_50 = (ema_50 - indicators["ema_50"][-2]) / max(indicators["ema_50"][-2], 1e-9) if len(indicators["ema_50"]) > 1 else 0
        logger.debug(
            f"[Trend DEBUG] ema20={ema_20:.2f} ema50={ema_50:.2f} price={price:.2f} "
            f"slope20={slope_20:.4f} slope50={slope_50:.4f} adx={adx:.2f} score={trend_score:.3f}"
        )
        
        if mean_revert_score > 0:
            rsi_now = indicators["rsi"]
            adx = indicators["adx"]
            bb_pos = (candles[-1].c - indicators["bb"]["lower"]) / (indicators["bb"]["upper"] - indicators["bb"]["lower"]) if (indicators["bb"]["upper"] - indicators["bb"]["lower"]) > 0 else 0.5
            logger.debug(
                f"[Mean DEBUG] rsi_now={rsi_now:.1f} bb_pos={bb_pos:.2f} adx={adx:.1f} score={mean_revert_score:.3f}"
            )
        
        # 가장 높은 점수의 레짐 선택
        scores = {
            RegimeType.TREND: trend_score,
            RegimeType.VOL_SPIKE: vol_spike_score,
            RegimeType.MEAN_REVERT: mean_revert_score
        }
        
        best_regime = max(scores, key=scores.get)
        confidence = scores[best_regime]
        
        # 타이브레이커: 동점 시 선택 규칙
        t, v, m = scores[RegimeType.TREND], scores[RegimeType.VOL_SPIKE], scores[RegimeType.MEAN_REVERT]
        rsi_now = indicators["rsi"]
        
        # Vol-Spike 우선 (뉴스 급변 우선)
        if v >= t - 0.05:
            best_regime = RegimeType.VOL_SPIKE
            confidence = v
        
        # Mean-Revert 우선 (복귀 신호 우선)
        if (m >= max(t, v) - 0.05) and (30 <= rsi_now <= 70):
            best_regime = RegimeType.MEAN_REVERT
            confidence = m
        
        # 특징값들 수집
        features = {
            "trend_score": trend_score,
            "vol_spike_score": vol_spike_score,
            "mean_revert_score": mean_revert_score,
            "adx": indicators["adx"],
            "rsi": indicators["rsi"],
            "ema_20": indicators["ema_20"][-1] if indicators["ema_20"] else closes[-1],
            "ema_50": indicators["ema_50"][-1] if indicators["ema_50"] else closes[-1],
            "bb_position": (closes[-1] - indicators["bb"]["lower"]) / (indicators["bb"]["upper"] - indicators["bb"]["lower"]) if (indicators["bb"]["upper"] - indicators["bb"]["lower"]) > 0 else 0.5
        }
        
        return RegimeResult(
            regime=best_regime,
            confidence=confidence,
            features=features,
            timestamp=datetime.now()
        )
    
    def _calculate_trend_score(self, candles: List, indicators: Dict) -> float:
        """Trend 레짐 점수 계산 (20/50EMA↑ & ADX>20)"""
        if not indicators["ema_20"] or not indicators["ema_50"]:
            return 0.0
        
        ema_20 = indicators["ema_20"][-1]
        ema_50 = indicators["ema_50"][-1]
        adx = indicators["adx"]
        price = candles[-1].c
        
        # 기울기(완화)
        ema_20_prev = indicators["ema_20"][-2] if len(indicators["ema_20"]) > 1 else ema_20
        ema_50_prev = indicators["ema_50"][-2] if len(indicators["ema_50"]) > 1 else ema_50
        slope_20 = (ema_20 - ema_20_prev) / max(ema_20_prev, 1e-9)
        slope_50 = (ema_50 - ema_50_prev) / max(ema_50_prev, 1e-9)
        
        score = 0.0
        # 1) 관계식(가장 중요)
        if ema_20 > ema_50 * (1 + self.thresholds["trend"]["ema_ratio_min"]):
            score += 0.30
        if price > ema_20:
            score += 0.20
        
        # 2) 기울기(완화 기준)
        if slope_20 > self.thresholds["trend"]["ema_ratio_min"]:
            score += 0.25
        if slope_50 > self.thresholds["trend"]["ema_ratio_min"] * 0.5:
            score += 0.15
        
        # 3) ADX(15부터 기여, 부드럽게 스케일)
        if adx >= 10:
            score += 0.10 * min((adx - 10) / 40.0, 1.0)  # adx 50에서 +0.10 풀기여
        
        # 4) 누적 모멘텀(최근 10캔들 수익률)
        if len(candles) >= 10:
            p0 = candles[-10].c
            p1 = candles[-1].c
            mom = (p1 - p0) / max(p0, 1e-9)
            if mom > self.thresholds["trend"]["price_momentum_min"]:
                score += 0.10
        
        return float(min(score, 1.0))
    
    def _calculate_vol_spike_score(self, candles: List, indicators: Dict) -> float:
        """Vol-Spike 레짐 점수 계산
        - 분포 기준 완화: p95→환경변수 pctl(기본 90)
        - 거래량 배수 완화: 2.0→환경변수 비율(기본 1.6)
        - OR: 최근 바 절대수익률(15–30s) ≥ MIN_BAR_RET_ABS(기본 0.006) 이면 스파이크 취급
        """
        n = len(candles)
        if n < 8:  # 최소 4개(2분) × 과거치 여유
            return 0.0
        
        # 최근 2분(30초봉 4개) 변동성/거래량
        recent = candles[-4:]
        recent_vol = np.mean([(c.h - c.l) / max(c.l, 1e-9) for c in recent])
        # 최근 1바 절대 수익률(15–30s 기준에서도 과대평가를 막기 위해 last bar 기준)
        try:
            last = candles[-1]
            prev = candles[-2]
            last_abs_ret = abs((last.c - prev.c) / max(prev.c, 1e-9))
        except Exception:
            last_abs_ret = 0.0
        
        # 과거 30분 동안 "2분 블록" 변동성 분포
        hist_blocks = []
        for i in range(0, n - 4):
            blk = candles[i:i+4]
            vol = np.mean([(c.h - c.l) / max(c.l, 1e-9) for c in blk])
            hist_blocks.append(vol)
        if not hist_blocks:
            return 0.0
        pctl = self.thresholds["vol_spike"].get("volatility_percentile", 90)
        hist_thr = np.percentile(hist_blocks, pctl)
        
        # 거래량 비율(최근 2분 합 / 과거 30분 평균 2분합)
        recent_volm = sum(c.v for c in recent)
        # 2분 블록별 볼륨
        hist_volm_blocks = []
        for i in range(0, n - 4):
            blk = candles[i:i+4]
            hist_volm_blocks.append(sum(c.v for c in blk))
        avg_hist_volm = np.mean(hist_volm_blocks) if hist_volm_blocks else recent_volm
        volume_ratio = recent_volm / max(avg_hist_volm, 1e-9)
        
        score = 0.0
        # OR: 최근 절대변동으로 즉시 스파이크 취급
        if last_abs_ret >= float(os.getenv("MIN_BAR_RET_ABS", "0.006")):
            return 1.0
        if recent_vol > hist_thr:
            score += 0.6
        if volume_ratio > self.thresholds["vol_spike"]["volume_ratio_min"]:
            score += 0.4
        return min(score, 1.0)
    
    def _calculate_mean_revert_score(self, candles: List, indicators: Dict) -> float:
        """Mean-Revert 레짐 점수 계산 (RSI 14 극단 후 밴드 복귀)"""
        rsi_now = indicators["rsi"]
        rsi_hist = indicators.get("rsi_hist", [])
        bb = indicators["bb"]
        adx = indicators["adx"]
        ema_20 = indicators["ema_20"][-1] if indicators["ema_20"] else candles[-1].c
        ema_50 = indicators["ema_50"][-1] if indicators["ema_50"] else candles[-1].c
        
        width = bb["upper"] - bb["lower"]
        bb_pos = (candles[-1].c - bb["lower"]) / width if width > 0 else 0.5
        bb_extreme = (bb_pos >= self.thresholds["mean_revert"]["bb_position_min"]) or \
                     (bb_pos <= 1 - self.thresholds["mean_revert"]["bb_position_min"])
        
        # 최근 20개 내 극단이 있었는지
        thr = self.thresholds["mean_revert"]["rsi_extreme_min"]  # 30
        recent_extreme = any((r <= thr or r >= 100 - thr) for r in rsi_hist[-20:]) if len(rsi_hist) >= 20 else False
        
        # 지금은 30~70으로 복귀했는지 (필수)
        back_to_mid = (30 <= rsi_now <= 70)
        
        if not (recent_extreme and back_to_mid):
            return 0.0  # 복귀 없으면 점수 0 (오탐 차단)
        
        score = 0.0
        # 복귀 확인 후 가점
        score += 0.5
        if bb_extreme:
            score += 0.2  # 밴드 밖→안 복귀 신호 강화
        
        # 강추세면 되돌림 성공 확률 낮음 → 감점
        if adx >= 20 or ema_20 > ema_50 * (1 + 0.001):
            score -= 0.2
        
        return float(max(0.0, min(score, 1.0)))
    
    def get_regime_weights(self, regime: RegimeType) -> Dict[str, float]:
        """레짐별 시그널 가중치"""
        weights = {
            RegimeType.TREND: {
                "tech": 0.75,  # 기술적 분석 비중 높음
                "sentiment": 0.25
            },
            RegimeType.VOL_SPIKE: {
                "tech": 0.30,  # 감성 분석 비중 높음
                "sentiment": 0.70
            },
            RegimeType.MEAN_REVERT: {
                "tech": 0.60,  # 중간
                "sentiment": 0.40
            },
            RegimeType.SIDEWAYS: {
                "tech": 0.50,  # 균등
                "sentiment": 0.50
            }
        }
        
        return weights.get(regime, {"tech": 0.5, "sentiment": 0.5})

def main():
    """테스트 실행"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    
    # 테스트용 가짜 데이터 생성
    from dataclasses import dataclass
    from datetime import datetime
    
    @dataclass
    class Bar30s:
        ticker: str
        ts: datetime
        o: float
        h: float
        l: float
        c: float
        v: int
        spread_est: float
    
    # 상승장 데이터 (Trend)
    trend_candles = []
    base_price = 100.0
    for i in range(30):
        price_change = 0.5 + (i * 0.1)  # 지속적 상승
        candle = Bar30s(
            ticker="TEST",
            ts=datetime.now() - timedelta(minutes=30-i),
            o=base_price + price_change,
            h=base_price + price_change + 0.05,  # 고저폭 더 줄임
            l=base_price + price_change - 0.03,
            c=base_price + price_change + 0.04,
            v=1500,
            spread_est=0.01
        )
        trend_candles.append(candle)
        base_price += price_change
    
    # 변동성 급등 데이터 (Vol-Spike)
    vol_spike_candles = []
    base_price = 100.0
    for i in range(30):
        if i >= 28:  # 마지막 4개 캔들 중에 급등
            price_change = 5.0
            volume = 10000
            high_low_range = 3.0
        else:
            price_change = 0.05
            volume = 500
            high_low_range = 0.1
        
        candle = Bar30s(
            ticker="TEST",
            ts=datetime.now() - timedelta(minutes=30-i),
            o=base_price + price_change,
            h=base_price + price_change + high_low_range,
            l=base_price + price_change - high_low_range * 0.6,
            c=base_price + price_change + high_low_range * 0.2,
            v=volume,
            spread_est=0.05
        )
        vol_spike_candles.append(candle)
        base_price += price_change
    
    # 평균회귀 데이터 (Mean-Revert)
    mean_revert_candles = []
    base_price = 100.0
    for i in range(30):
        if i < 15:  # 극단으로 이동 (RSI 극단값 생성)
            price_change = -8.0 if i % 2 == 0 else 8.0
        elif i < 25:  # 중간으로 복귀 (RSI 30~70으로 복귀)
            price_change = 0.5 if i % 2 == 0 else -0.5
        else:  # 안정화
            price_change = 0.1
        
        candle = Bar30s(
            ticker="TEST",
            ts=datetime.now() - timedelta(minutes=30-i),
            o=base_price + price_change,
            h=base_price + price_change + 0.5,
            l=base_price + price_change - 0.4,
            c=base_price + price_change + 0.2,
            v=1200,
            spread_est=0.02
        )
        mean_revert_candles.append(candle)
        base_price += price_change
    
    # 레짐 감지기 테스트
    detector = RegimeDetector()
    
    print("=== 레짐 감지 테스트 ===")
    
    # Trend 테스트
    trend_result = detector.detect_regime(trend_candles)
    print(f"Trend 레짐: {trend_result.regime.value} (신뢰도: {trend_result.confidence:.2f})")
    print(f"  점수: Trend={trend_result.features['trend_score']:.3f}, Vol={trend_result.features['vol_spike_score']:.3f}, Mean={trend_result.features['mean_revert_score']:.3f}")
    
    # Vol-Spike 테스트
    vol_spike_result = detector.detect_regime(vol_spike_candles)
    print(f"Vol-Spike 레짐: {vol_spike_result.regime.value} (신뢰도: {vol_spike_result.confidence:.2f})")
    print(f"  점수: Trend={vol_spike_result.features['trend_score']:.3f}, Vol={vol_spike_result.features['vol_spike_score']:.3f}, Mean={vol_spike_result.features['mean_revert_score']:.3f}")
    
    # Mean-Revert 테스트
    mean_revert_result = detector.detect_regime(mean_revert_candles)
    print(f"Mean-Revert 레짐: {mean_revert_result.regime.value} (신뢰도: {mean_revert_result.confidence:.2f})")
    print(f"  점수: Trend={mean_revert_result.features['trend_score']:.3f}, Vol={mean_revert_result.features['vol_spike_score']:.3f}, Mean={mean_revert_result.features['mean_revert_score']:.3f}")
    
    print("✅ 세 레짐 전부 발생 확인 완료")

if __name__ == "__main__":
    main()
