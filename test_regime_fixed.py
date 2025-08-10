#!/usr/bin/env python3
"""
레짐 감지 정확성 테스트 - 원래 요구사항 검증
"""
import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import logging
from datetime import datetime, timedelta
from dataclasses import dataclass

from app.engine.regime import RegimeDetector, RegimeType

# 테스트용 Bar30s 클래스
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

def create_trend_data():
    """Trend 레짐용 데이터: 20/50EMA↑ & ADX>20"""
    candles = []
    base_price = 100.0
    
    # 지속적 상승으로 EMA 20 > EMA 50 만들기
    for i in range(30):
        # 점진적 상승 (EMA 20이 50보다 빠르게 상승)
        price_change = 0.5 + (i * 0.3)  # 지속적 상승
        candle = Bar30s(
            ticker="AAPL",
            ts=datetime.now() - timedelta(minutes=30-i),
            o=base_price + price_change,
            h=base_price + price_change + 0.1,  # 작은 변동성
            l=base_price + price_change - 0.05,
            c=base_price + price_change + 0.08,
            v=1000,
            spread_est=0.01
        )
        candles.append(candle)
        base_price += price_change
    
    return candles

def create_vol_spike_data():
    """Vol-Spike 레짐용 데이터: 1분 실현변동성 상위 5%"""
    candles = []
    base_price = 100.0
    
    # 대부분 평온한 상태
    for i in range(25):
        price_change = 0.1
        candle = Bar30s(
            ticker="TSLA",
            ts=datetime.now() - timedelta(minutes=30-i),
            o=base_price + price_change,
            h=base_price + price_change + 0.05,  # 작은 변동성
            l=base_price + price_change - 0.03,
            c=base_price + price_change + 0.02,
            v=500,
            spread_est=0.01
        )
        candles.append(candle)
        base_price += price_change
    
    # 마지막 5개에서 급등
    for i in range(5):
        price_change = 2.0 + (i * 0.5)
        candle = Bar30s(
            ticker="TSLA",
            ts=datetime.now() - timedelta(minutes=5-i),
            o=base_price + price_change,
            h=base_price + price_change + 1.0,  # 큰 변동성
            l=base_price + price_change - 0.8,
            c=base_price + price_change + 0.6,
            v=5000,  # 거래량 급증
            spread_est=0.05
        )
        candles.append(candle)
        base_price += price_change
    
    return candles

def create_mean_revert_data():
    """Mean-Revert 레짐용 데이터: RSI 14 극단 후 밴드 복귀"""
    candles = []
    base_price = 100.0
    
    # RSI 극단값으로 이동 (과매수/과매도)
    for i in range(20):
        if i < 10:
            # 과매수로 이동
            price_change = 2.0 + (i * 0.5)
        else:
            # 과매도로 이동
            price_change = -3.0 - (i * 0.3)
        
        candle = Bar30s(
            ticker="MSFT",
            ts=datetime.now() - timedelta(minutes=30-i),
            o=base_price + price_change,
            h=base_price + price_change + 0.3,
            l=base_price + price_change - 0.2,
            c=base_price + price_change + 0.1,
            v=1000,
            spread_est=0.02
        )
        candles.append(candle)
        base_price += price_change
    
    # 극단에서 중간으로 복귀
    for i in range(10):
        price_change = 0.1  # 안정화
        candle = Bar30s(
            ticker="MSFT",
            ts=datetime.now() - timedelta(minutes=10-i),
            o=base_price + price_change,
            h=base_price + price_change + 0.1,
            l=base_price + price_change - 0.05,
            c=base_price + price_change + 0.05,
            v=1000,
            spread_est=0.01
        )
        candles.append(candle)
        base_price += price_change
    
    return candles

def main():
    """정확성 테스트"""
    logging.basicConfig(level=logging.INFO)
    
    detector = RegimeDetector()
    
    print("=== 레짐 감지 정확성 테스트 ===")
    print()
    
    # 1. Trend 테스트
    print("1. Trend 레짐 테스트 (20/50EMA↑ & ADX>20)")
    print("-" * 50)
    trend_candles = create_trend_data()
    trend_result = detector.detect_regime(trend_candles)
    print(f"감지된 레짐: {trend_result.regime.value}")
    print(f"신뢰도: {trend_result.confidence:.3f}")
    print(f"특징값: {trend_result.features}")
    print()
    
    # 2. Vol-Spike 테스트
    print("2. Vol-Spike 레짐 테스트 (1분 실현변동성 상위 5%)")
    print("-" * 50)
    vol_spike_candles = create_vol_spike_data()
    vol_spike_result = detector.detect_regime(vol_spike_candles)
    print(f"감지된 레짐: {vol_spike_result.regime.value}")
    print(f"신뢰도: {vol_spike_result.confidence:.3f}")
    print(f"특징값: {vol_spike_result.features}")
    print()
    
    # 3. Mean-Revert 테스트
    print("3. Mean-Revert 레짐 테스트 (RSI 14 극단 후 밴드 복귀)")
    print("-" * 50)
    mean_revert_candles = create_mean_revert_data()
    mean_revert_result = detector.detect_regime(mean_revert_candles)
    print(f"감지된 레짐: {mean_revert_result.regime.value}")
    print(f"신뢰도: {mean_revert_result.confidence:.3f}")
    print(f"특징값: {mean_revert_result.features}")
    print()
    
    # 4. 정확성 검증
    print("4. 정확성 검증")
    print("-" * 50)
    
    expected_regimes = ["trend", "vol_spike", "mean_revert"]
    actual_regimes = [
        trend_result.regime.value,
        vol_spike_result.regime.value,
        mean_revert_result.regime.value
    ]
    
    correct_count = 0
    for expected, actual in zip(expected_regimes, actual_regimes):
        if expected == actual:
            correct_count += 1
            print(f"✅ {expected}: 정확")
        else:
            print(f"❌ {expected}: {actual}로 잘못 감지")
    
    accuracy = correct_count / len(expected_regimes) * 100
    print(f"\n정확도: {accuracy:.1f}% ({correct_count}/{len(expected_regimes)})")
    
    if accuracy >= 80:
        print("✅ 레짐 감지 정확성 검증 통과")
    else:
        print("❌ 레짐 감지 정확성 검증 실패")

if __name__ == "__main__":
    main()
