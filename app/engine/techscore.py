"""
TechScore 엔진 - Week 1 라이트봇 A단계
EMA/MACD/RSI/VWAP 편차 → 0~1 정규화
"""
import numpy as np
import pandas as pd
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)

@dataclass
class TechScoreResult:
    """TechScore 계산 결과"""
    score: float  # 0~1 정규화된 점수
    components: Dict[str, float]  # 각 지표별 점수
    timestamp: datetime

class TechScoreEngine:
    """TechScore 엔진 - Week 1 버전"""
    
    def __init__(self):
        """TechScore 엔진 초기화"""
        # 지표별 가중치
        self.weights = {
            "ema": 0.25,      # EMA 상승/하락
            "macd": 0.25,     # MACD 신호
            "rsi": 0.25,      # RSI 위치
            "vwap": 0.25      # VWAP 편차
        }
        
        # 정규화 범위
        self.normalization_ranges = {
            "ema": (-0.05, 0.05),    # -5% ~ +5%
            "macd": (-2.0, 2.0),     # MACD 값 범위
            "rsi": (20, 80),         # RSI 범위
            "vwap": (-0.03, 0.03)    # VWAP 편차 -3% ~ +3%
        }
        
        logger.info("TechScore 엔진 초기화 완료")
    
    def calculate_ema(self, prices: List[float], period: int) -> List[float]:
        """EMA 계산"""
        if len(prices) < period:
            return []
        
        alpha = 2.0 / (period + 1)
        ema = [prices[0]]
        
        for price in prices[1:]:
            ema.append(alpha * price + (1 - alpha) * ema[-1])
        
        return ema
    
    def calculate_macd(self, prices: List[float], fast: int = 12, slow: int = 26, signal: int = 9) -> Dict[str, float]:
        """MACD 계산"""
        if len(prices) < slow:
            return {"macd": 0.0, "signal": 0.0, "histogram": 0.0}
        
        ema_fast = self.calculate_ema(prices, fast)
        ema_slow = self.calculate_ema(prices, slow)
        
        if not ema_fast or not ema_slow:
            return {"macd": 0.0, "signal": 0.0, "histogram": 0.0}
        
        # MACD 라인
        macd_line = []
        for i in range(len(ema_slow)):
            if i < len(ema_fast):
                macd_line.append(ema_fast[i] - ema_slow[i])
            else:
                macd_line.append(0.0)
        
        # Signal 라인 (MACD의 EMA)
        signal_line = self.calculate_ema(macd_line, signal)
        
        # Histogram
        histogram = []
        for i in range(len(signal_line)):
            if i < len(macd_line):
                histogram.append(macd_line[i] - signal_line[i])
            else:
                histogram.append(0.0)
        
        return {
            "macd": macd_line[-1] if macd_line else 0.0,
            "signal": signal_line[-1] if signal_line else 0.0,
            "histogram": histogram[-1] if histogram else 0.0
        }
    
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
    
    def calculate_vwap(self, candles: List) -> float:
        """VWAP 계산"""
        if not candles:
            return 0.0
        
        total_pv = 0.0  # Price * Volume
        total_volume = 0.0
        
        for candle in candles:
            # 캔들 중간가격
            typical_price = (candle.h + candle.l + candle.c) / 3
            total_pv += typical_price * candle.v
            total_volume += candle.v
        
        if total_volume == 0:
            return candles[-1].c
        
        return total_pv / total_volume
    
    def normalize_value(self, value: float, min_val: float, max_val: float) -> float:
        """값을 0~1 범위로 정규화"""
        if max_val == min_val:
            return 0.5
        
        normalized = (value - min_val) / (max_val - min_val)
        return max(0.0, min(1.0, normalized))
    
    def calculate_ema_score(self, prices: List[float]) -> float:
        """EMA 점수 계산"""
        if len(prices) < 20:
            return 0.5
        
        ema_20 = self.calculate_ema(prices, 20)
        ema_50 = self.calculate_ema(prices, 50)
        
        if not ema_20 or not ema_50:
            return 0.5
        
        # EMA 20과 50의 비율
        ema_ratio = (ema_20[-1] - ema_50[-1]) / ema_50[-1] if ema_50[-1] > 0 else 0
        
        # 정규화
        min_val, max_val = self.normalization_ranges["ema"]
        score = self.normalize_value(ema_ratio, min_val, max_val)
        
        return score
    
    def calculate_macd_score(self, prices: List[float]) -> float:
        """MACD 점수 계산"""
        if len(prices) < 26:
            return 0.5
        
        macd_data = self.calculate_macd(prices)
        
        # MACD 히스토그램 사용 (신호선 대비 MACD 위치)
        histogram = macd_data["histogram"]
        
        # 정규화
        min_val, max_val = self.normalization_ranges["macd"]
        score = self.normalize_value(histogram, min_val, max_val)
        
        return score
    
    def calculate_rsi_score(self, prices: List[float]) -> float:
        """RSI 점수 계산"""
        if len(prices) < 15:
            return 0.5
        
        rsi = self.calculate_rsi(prices, 14)
        
        # RSI를 0~1로 변환 (50이 중간, 80 이상이 좋음, 20 이하가 나쁨)
        if rsi >= 50:
            # 50~100 범위를 0.5~1.0으로 매핑
            score = 0.5 + (rsi - 50) / 50 * 0.5
        else:
            # 0~50 범위를 0.0~0.5로 매핑
            score = rsi / 50 * 0.5
        
        return max(0.0, min(1.0, score))
    
    def calculate_vwap_score(self, candles: List) -> float:
        """VWAP 점수 계산"""
        if not candles:
            return 0.5
        
        vwap = self.calculate_vwap(candles)
        current_price = candles[-1].c
        
        # VWAP 대비 편차
        vwap_deviation = (current_price - vwap) / vwap if vwap > 0 else 0
        
        # 정규화 (VWAP 위에 있으면 좋음)
        min_val, max_val = self.normalization_ranges["vwap"]
        score = self.normalize_value(vwap_deviation, min_val, max_val)
        
        return score
    
    def calculate_tech_score(self, candles: List) -> TechScoreResult:
        """
        TechScore 계산
        
        Args:
            candles: OHLCV 캔들 리스트 (Bar30s 객체들)
            
        Returns:
            TechScoreResult: 계산된 TechScore
        """
        if len(candles) < 20:
            return TechScoreResult(
                score=0.5,
                components={"ema": 0.5, "macd": 0.5, "rsi": 0.5, "vwap": 0.5},
                timestamp=datetime.now()
            )
        
        # 가격 데이터 추출
        prices = [c.c for c in candles]
        
        # 각 지표별 점수 계산
        ema_score = self.calculate_ema_score(prices)
        macd_score = self.calculate_macd_score(prices)
        rsi_score = self.calculate_rsi_score(prices)
        vwap_score = self.calculate_vwap_score(candles)
        
        # 가중 평균으로 최종 점수 계산
        final_score = (
            ema_score * self.weights["ema"] +
            macd_score * self.weights["macd"] +
            rsi_score * self.weights["rsi"] +
            vwap_score * self.weights["vwap"]
        )
        
        components = {
            "ema": ema_score,
            "macd": macd_score,
            "rsi": rsi_score,
            "vwap": vwap_score
        }
        
        return TechScoreResult(
            score=final_score,
            components=components,
            timestamp=datetime.now()
        )
    
    def get_score_interpretation(self, score: float) -> str:
        """TechScore 해석"""
        if score >= 0.8:
            return "매우 강한 상승 신호"
        elif score >= 0.6:
            return "강한 상승 신호"
        elif score >= 0.4:
            return "중립"
        elif score >= 0.2:
            return "강한 하락 신호"
        else:
            return "매우 강한 하락 신호"

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
    
    # 상승장 데이터
    bullish_candles = []
    base_price = 100.0
    for i in range(30):
        price_change = 0.5 + (i * 0.2)  # 지속적 상승
        candle = Bar30s(
            ticker="TEST",
            ts=datetime.now() - timedelta(minutes=30-i),
            o=base_price + price_change,
            h=base_price + price_change + 0.3,
            l=base_price + price_change - 0.1,
            c=base_price + price_change + 0.2,
            v=1000,
            spread_est=0.01
        )
        bullish_candles.append(candle)
        base_price += price_change
    
    # 하락장 데이터
    bearish_candles = []
    base_price = 100.0
    for i in range(30):
        price_change = -0.3 - (i * 0.1)  # 지속적 하락
        candle = Bar30s(
            ticker="TEST",
            ts=datetime.now() - timedelta(minutes=30-i),
            o=base_price + price_change,
            h=base_price + price_change + 0.1,
            l=base_price + price_change - 0.3,
            c=base_price + price_change - 0.2,
            v=1000,
            spread_est=0.01
        )
        bearish_candles.append(candle)
        base_price += price_change
    
    # TechScore 엔진 테스트
    engine = TechScoreEngine()
    
    print("=== TechScore 테스트 ===")
    
    # 상승장 테스트
    bullish_result = engine.calculate_tech_score(bullish_candles)
    print(f"상승장 TechScore: {bullish_result.score:.3f}")
    print(f"해석: {engine.get_score_interpretation(bullish_result.score)}")
    print(f"구성요소: EMA={bullish_result.components['ema']:.3f}, MACD={bullish_result.components['macd']:.3f}, RSI={bullish_result.components['rsi']:.3f}, VWAP={bullish_result.components['vwap']:.3f}")
    
    # 하락장 테스트
    bearish_result = engine.calculate_tech_score(bearish_candles)
    print(f"하락장 TechScore: {bearish_result.score:.3f}")
    print(f"해석: {engine.get_score_interpretation(bearish_result.score)}")
    print(f"구성요소: EMA={bearish_result.components['ema']:.3f}, MACD={bearish_result.components['macd']:.3f}, RSI={bearish_result.components['rsi']:.3f}, VWAP={bearish_result.components['vwap']:.3f}")
    
    # 상승장일수록 TechScore가 높은지 확인
    if bullish_result.score > bearish_result.score:
        print("✅ 상승장일수록 TechScore↑ 확인 완료")
    else:
        print("❌ 상승장일수록 TechScore↑ 확인 실패")
    
    print("✅ TechScore 테스트 완료")

if __name__ == "__main__":
    main()
