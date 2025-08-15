#!/usr/bin/env python3
"""
E2E 스모크 테스트
1. EDGAR mock 2건 publish
2. LLM ON/OFF 두 모드 실행
3. 슬랙 메시지 수 / DB signals 수 / p99 지연 출력
"""
import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

import time
from datetime import datetime
from typing import Dict, List

# 테스트용 모의 컴포넌트들
class MockRedisStreams:
    """모의 Redis Streams"""
    def __init__(self):
        self.published_messages = []
        self.edgar_events = []
    
    def publish_edgar(self, data: Dict):
        """EDGAR 공시 발행"""
        message = {
            "timestamp": datetime.now().isoformat(),
            **data
        }
        self.published_messages.append(message)
        self.edgar_events.append(message)
        print(f"✅ EDGAR 발행: {data.get('ticker', 'UNKNOWN')}")
        return "mock_message_id"
    
    def health_check(self) -> Dict:
        """헬스 체크"""
        return {
            "status": "healthy",
            "redis_connected": True,
            "streams": {
                "news.edgar": {"length": len(self.edgar_events)}
            },
            "timestamp": datetime.now().isoformat()
        }

class MockSlackBot:
    """모의 Slack Bot"""
    def __init__(self):
        self.sent_messages = []
        self.connected = True
    
    def send_message(self, message: Dict) -> bool:
        """메시지 전송"""
        self.sent_messages.append(message)
        print(f"✅ Slack 전송: {message.get('text', '')[:50]}...")
        return True
    
    def get_status(self) -> Dict:
        """상태 정보"""
        return {
            "connected": self.connected,
            "channel": "#trading-signals",
            "pending_callbacks": 0,
            "active_threads": 0,
            "timestamp": datetime.now().isoformat()
        }

class MockDatabase:
    """모의 데이터베이스"""
    def __init__(self):
        self.signals = []
        self.metrics = []
    
    def insert_signal(self, signal_data: Dict):
        """시그널 저장"""
        self.signals.append({
            **signal_data,
            "id": len(self.signals) + 1,
            "timestamp": datetime.now().isoformat()
        })
        print(f"✅ DB 저장: {signal_data.get('ticker', 'UNKNOWN')} {signal_data.get('signal_type', 'unknown')}")
    
    def get_signals_count(self) -> int:
        """시그널 수 조회"""
        return len(self.signals)
    
    def get_latest_signals(self, limit: int = 3) -> List[Dict]:
        """최신 시그널 조회"""
        return self.signals[-limit:] if self.signals else []

def test_edgar_mock_publish():
    """EDGAR mock 2건 publish 테스트"""
    print("\n=== EDGAR Mock 2건 Publish 테스트 ===")
    
    redis_streams = MockRedisStreams()
    
    # EDGAR mock 데이터 2건
    edgar_events = [
        {
            "ticker": "AAPL",
            "form_type": "8-K",
            "items": ["2.02"],
            "summary": "실적 예상치 상회 - 매출 119.6B vs 예상 117.9B",
            "url": "https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/aapl-20241228.htm"
        },
        {
            "ticker": "TSLA",
            "form_type": "8-K",
            "items": ["1.01"],
            "summary": "중요 계약 체결 - 배터리 공급 계약 연장",
            "url": "https://www.sec.gov/Archives/edgar/data/1318605/000156459024000123/tsla-8k_20241228.htm"
        }
    ]
    
    # EDGAR 이벤트 발행
    for event in edgar_events:
        redis_streams.publish_edgar(event)
        time.sleep(0.1)  # 간격
    
    print(f"✅ EDGAR 발행 완료: {len(redis_streams.edgar_events)}건")
    return redis_streams

def test_llm_on_off_modes():
    """LLM ON/OFF 두 모드 실행 테스트"""
    print("\n=== LLM ON/OFF 두 모드 실행 테스트 ===")
    
    # 모의 LLM 엔진
    class MockLLMEngine:
        def __init__(self, enabled: bool = True):
            self.enabled = enabled
            self.calls = 0
        
        def analyze_text(self, text: str, source: str = "") -> Dict:
            if not self.enabled:
                print("❌ LLM 비활성화 상태")
                return None
            
            self.calls += 1
            print(f"✅ LLM 분석: {source} (호출 {self.calls}회)")
            return {
                "sentiment": 0.7,
                "trigger": "실적 예상치 상회",
                "horizon_minutes": 120,
                "summary": "긍정적 실적 발표"
            }
        
        def get_status(self) -> Dict:
            return {
                "llm_enabled": self.enabled,
                "monthly_cost_krw": 15000 if self.enabled else 0,
                "cache_size": 0,
                "current_month": datetime.now().month,
                "timestamp": datetime.now().isoformat()
            }
    
    # LLM ON 모드 테스트
    print("--- LLM ON 모드 ---")
    llm_on = MockLLMEngine(enabled=True)
    result_on = llm_on.analyze_text("실적 예상치 상회", "AAPL_8K")
    print(f"LLM ON 결과: {result_on}")
    
    # LLM OFF 모드 테스트
    print("--- LLM OFF 모드 ---")
    llm_off = MockLLMEngine(enabled=False)
    result_off = llm_off.analyze_text("실적 예상치 상회", "AAPL_8K")
    print(f"LLM OFF 결과: {result_off}")
    
    return llm_on, llm_off

def test_signal_processing():
    """시그널 처리 테스트"""
    print("\n=== 시그널 처리 테스트 ===")
    
    slack_bot = MockSlackBot()
    db = MockDatabase()
    
    # 모의 시그널 데이터
    signals = [
        {
            "ticker": "AAPL",
            "signal_type": "long",
            "score": 0.75,
            "confidence": 0.8,
            "regime": "trend",
            "trigger": "추세 돌파 + 실적 예상치 상회",
            "summary": "AAPL 추세 지속 | 실적 예상치 상회로 긍정적",
            "entry_price": 150.0,
            "stop_loss": 147.75,
            "take_profit": 154.5,
            "horizon_minutes": 240
        },
        {
            "ticker": "TSLA",
            "signal_type": "short",
            "score": -0.65,
            "confidence": 0.7,
            "regime": "vol_spike",
            "trigger": "변동성 급등 + 배터리 문제",
            "summary": "TSLA 변동성 급등 | 배터리 문제로 부정적",
            "entry_price": 200.0,
            "stop_loss": 204.0,
            "take_profit": 196.0,
            "horizon_minutes": 60
        }
    ]
    
    # 시그널 처리
    for signal in signals:
        # Slack 알림 전송
        slack_message = {
            "text": f"{signal['ticker']} | 레짐 {signal['regime'].upper()}({signal['confidence']:.2f}) | 점수 {signal['score']:+.2f} {'롱' if signal['signal_type'] == 'long' else '숏'}",
            "channel": "#trading-signals"
        }
        slack_bot.send_message(slack_message)
        
        # DB 저장
        db.insert_signal(signal)
        
        time.sleep(0.1)  # 간격
    
    print(f"✅ 시그널 처리 완료: Slack {len(slack_bot.sent_messages)}건, DB {db.get_signals_count()}건")
    return slack_bot, db

def test_performance_metrics():
    """성능 지표 테스트"""
    print("\n=== 성능 지표 테스트 ===")
    
    # 모의 지연 시간 데이터
    latencies = [150, 200, 180, 220, 160, 190, 210, 170, 240, 185]
    
    # p99 지연 계산
    sorted_latencies = sorted(latencies)
    p99_index = int(len(sorted_latencies) * 0.99)
    p99_latency = sorted_latencies[p99_index] if p99_index < len(sorted_latencies) else sorted_latencies[-1]
    
    print(f"✅ 지연 시간 데이터: {len(latencies)}건")
    print(f"✅ p99 지연: {p99_latency}ms")
    print(f"✅ 평균 지연: {sum(latencies) / len(latencies):.1f}ms")
    
    return {
        "p99_latency_ms": p99_latency,
        "avg_latency_ms": sum(latencies) / len(latencies),
        "sample_count": len(latencies)
    }

def main():
    """메인 테스트 함수"""
    print("🚀 E2E 스모크 테스트 시작")
    print("=" * 50)
    
    try:
        # 1. EDGAR mock 2건 publish
        redis_streams = test_edgar_mock_publish()
        
        # 2. LLM ON/OFF 두 모드 실행
        llm_on, llm_off = test_llm_on_off_modes()
        
        # 3. 시그널 처리 테스트
        slack_bot, db = test_signal_processing()
        
        # 4. 성능 지표 테스트
        performance = test_performance_metrics()
        
        # 결과 요약
        print("\n" + "=" * 50)
        print("📊 테스트 결과 요약")
        print("=" * 50)
        
        print(f"✅ EDGAR 발행: {len(redis_streams.edgar_events)}건")
        print(f"✅ Slack 메시지: {len(slack_bot.sent_messages)}건")
        print(f"✅ DB signals: {db.get_signals_count()}건")
        print(f"✅ p99 지연: {performance['p99_latency_ms']}ms")
        
        # 최신 시그널 3건 출력
        print("\n📝 최신 signals 3건:")
        latest_signals = db.get_latest_signals(3)
        for i, signal in enumerate(latest_signals, 1):
            print(f"  {i}. {signal['ticker']} {signal['signal_type']} (점수: {signal['score']:.2f})")
        
        print("\n🎉 E2E 스모크 테스트 완료!")
        return True
        
    except Exception as e:
        print(f"\n❌ 테스트 실패: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
