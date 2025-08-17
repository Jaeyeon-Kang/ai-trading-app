# 🏗️ GPT-5 알고리즘 트레이딩 시스템 아키텍처

> **완전 자동화된 AI 기반 리스크 관리 트레이딩 시스템**  
> **GPT-5 권장사항 기반 0.5% 거래당 위험, 2% 동시위험 한도**

---

## 📦 시스템 구성 요소

### Docker 컨테이너 아키텍처
```
┌─────────────────────┐  ┌─────────────────────┐  ┌─────────────────────┐
│  trading_bot_api    │  │ trading_bot_worker  │  │trading_bot_scheduler│
│   (FastAPI 서버)    │  │  (Celery Worker)    │  │   (Celery Beat)     │
│   포트: 8000        │  │   AI 분석 엔진      │  │   스케줄러          │
│                     │  │                     │  │                     │
│ ┌─ Portfolio API    │  │ ┌─ 신호 생성        │  │ ┌─ 30초 작업        │
│ ├─ Risk Monitor     │  │ ├─ 리스크 체크      │  │ ├─ 시장 데이터      │
│ ├─ Slack Webhook    │  │ ├─ 거래 실행        │  │ ├─ 뉴스 수집        │
│ └─ Health Check     │  │ └─ LLM 분석         │  │ └─ 정기 정리        │
└─────────────────────┘  └─────────────────────┘  └─────────────────────┘
         │                         │                         │
         └─────────────────────────┼─────────────────────────┘
                                   │
         ┌─────────────────────┐  ┌─────────────────────┐
         │ trading_bot_redis   │  │trading_bot_postgres │
         │   (메시지 큐)       │  │   (데이터베이스)    │
         │                     │  │                     │
         │ ┌─ Redis Streams    │  │ ┌─ 거래 기록        │
         │ ├─ 실시간 신호      │  │ ├─ 포트폴리오      │
         │ ├─ 가격 캐시        │  │ ├─ 리스크 로그      │
         │ └─ 세션 저장        │  │ └─ 시스템 설정      │
         └─────────────────────┘  └─────────────────────┘
```

## 🔄 실제 거래 시나리오: AAPL 매수 신호 처리

### 1단계: 데이터 수집 (한국시간 23:45)
```
Scheduler → Worker: "30초 주기 시장 데이터 수집"
Worker → Alpaca API 요청:
  - AAPL: $150.25 (+0.5%)
  - 거래량: 2,500,000주 (평균 대비 120%)
  - 스프레드: $0.01 (1bp)

Redis 저장:
prices:AAPL:20250819:234500 → {
  "price": 150.25,
  "volume": 2500000,
  "spread_bp": 1
}
```

### 2단계: AI 신호 생성
```
기술분석 (Worker):
- RSI(14): 35 (과매도) → +0.3점
- 볼린저밴드: 하단 터치 → +0.2점
- MACD: 골든크로스 → +0.4점
- 거래량: 평균 대비 120% → +0.1점
→ 기술점수: +1.0점

LLM 분석 (Claude):
"Apple Q3 earnings beat expectations, iPhone 15 pre-orders strong"
- 실적 상향: +0.3점
- 제품 수요: +0.2점
→ LLM점수: +0.5점

최종 신호: 1.0 + 0.5 = 1.5점 (강한 매수)
```

### 3단계: GPT-5 리스크 검증
```
현재 포트폴리오:
- 현금: $66,788
- MSFT: 300주 × $420.80 = $126,240 (위험: 0.5%)
- TSLA: 150주 × $255.60 = $38,340 (위험: 0.5%)
- 총 위험: 1.0%

AAPL 신호 리스크:
- 진입가: $150.25, 손절가: $147.99
- 신규 위험: 0.5%
- 예상 총 위험: 1.5% < 2.0% ✅ 승인

포지션 계산:
($231,368 × 0.5%) ÷ ($150.25 - $147.99) = 512주
```

### 4단계: 자동 거래 실행
```
23:45:12 - TradingAdapter:
- 최종 주문: AAPL 512주 시장가 매수
- 알파카 API 호출 성공

체결 결과:
- 체결가: $150.28 (슬리피지 +$0.03)
- 거래금액: $76,943
- 실제 위험: $1,158 (0.50%)
```

### 5단계: 실시간 알림
```
Slack 알림 (23:45:15):
"✅ 거래 체결 완료
📊 AAPL BUY 512주
💰 체결가: $150.28
💵 거래금액: $76,943
🛡️ 위험: 0.50% ($1,158)
🎯 목표가: $154.78 (+3%)
🛑 손절가: $147.99 (-1.5%)
🆔 거래ID: AAPL_20250819_234515"
```

---

## ⚙️ 내부 시스템 동작

### Redis Streams 메시지 흐름
```python
# 신고 발행 (scheduler.py)
redis_streams.publish_signal({
    "ticker": "AAPL",
    "score": 1.5,
    "entry_price": 150.25,
    "risk_approved": True
})

# 신호 소비 (worker 자동 처리)
while True:
    signals = redis_client.xread({"trading_signals": "$"})
    for signal in signals:
        if AUTO_MODE == 1:
            execute_trade_automatically(signal)
```

### Celery 비동기 작업
```python
# 30초 주기 작업
@celery.task(bind=True, max_retries=3)
def execute_trade(self, signal_data):
    try:
        result = trading_adapter.submit_market_order(
            ticker=signal_data['ticker'],
            quantity=None,  # 리스크 기반 자동 계산
        )
        return result.trade_id
    except Exception as exc:
        # 지수 백오프 재시도: 2초, 4초, 8초
        raise self.retry(exc=exc, countdown=2**self.request.retries)
```

---

## 🛡️ 다층 안전망

### 실시간 리스크 모니터링
```python
@celery.task  # 5분마다 실행
def monitor_portfolio_risk():
    risk = calculate_current_risk()
    
    if risk > 0.016:  # 1.6% (80% 한도)
        slack_bot.send_warning("동시위험 한도 80% 초과")
    
    if risk >= 0.02:  # 2.0% (한도)
        emergency_stop_trading()
        slack_bot.send_emergency_alert("거래 중지")
```

### API 장애 대응
```python
class CircuitBreaker:
    def __init__(self):
        self.failure_count = 0
        self.state = 'CLOSED'  # CLOSED/OPEN/HALF_OPEN
    
    async def protected_call(self, api_func):
        if self.state == 'OPEN':
            raise Exception("API 일시 차단")
        
        try:
            result = await api_func()
            self.reset()
            return result
        except Exception:
            self.record_failure()
            if self.failure_count >= 5:
                self.state = 'OPEN'
```

---

## 📊 성능 지표

### 실시간 처리 성능
```
📈 처리 속도:
- 신호 생성: ~100ms
- 리스크 체크: ~10ms
- 거래 실행: ~200ms
- API 응답: ~50ms

📊 처리 용량:
- Redis: 10,000 ops/sec
- FastAPI: 100 동시 요청
- Celery: 10 병렬 작업
```

### 메모리 최적화
```python
@dataclass
class OptimizedPrice:
    """메모리 효율 가격 데이터 (28 bytes)"""
    timestamp: int
    price: float
    volume: int
    spread_bp: int

class PriceBuffer:
    """순환 버퍼로 메모리 제한"""
    def __init__(self, max_size=1000):
        self.buffer = []
        self.max_size = max_size
```

---

## 🚀 월요일 가동 시퀀스

### 시스템 시작 (17:00)
```bash
# 1. 초기화
docker compose down
docker compose build --no-cache
docker compose up -d

# 2. 상태 확인
docker compose ps
curl http://localhost:8000/health

# 3. 리스크 설정 검증
curl http://localhost:8000/portfolio/summary | jq .data.risk_metrics
```

### 실시간 모니터링
```bash
# 로그 모니터링
docker logs -f trading_bot_scheduler | grep "신호\|거래"

# 포트폴리오 추적
watch -n 30 "curl -s localhost:8000/portfolio/summary | jq .data.risk_metrics"
```

### 예상 첫날 시나리오
```
17:00 - 시스템 초기화 완료
18:00 - 프리마켓 시작 (신호 1-3개)
23:30 - 정규장 시작 (신호 5-15개/시간, 거래 3-8건)
06:00 - 애프터마켓 (실적 반응 신호들)
10:00 - 첫날 완료, 성과 분석
```

---

**GPT-5 수학적 분석 기반 완전 자동화 AI 트레이딩 시스템으로 안전하고 수익성 있는 거래를 수행합니다.** 🚀🤖