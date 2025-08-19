# AI 트레이딩 시스템 심볼 라우팅 로직 재검토 요청

## 배경 상황

이전 GPT 분석 및 권장사항에 따라 **심볼 라우팅 시스템**을 구현했습니다. 그러나 구현 완료 후 심각한 논리적 문제점들을 발견했습니다.

## 현재 구현된 라우팅 시스템

### 구현 위치: `app/jobs/scheduler.py`

```python
# 심볼 라우팅 맵 정의
SYMBOL_ROUTING_MAP = {
    # NASDAQ-100 메가캡 → SQQQ (NASDAQ 인버스 ETF)
    "AAPL": "SQQQ",
    "MSFT": "SQQQ", 
    "TSLA": "SQQQ",
    "AMZN": "SQQQ",
    "META": "SQQQ",
    "GOOGL": "SQQQ",
    
    # 반도체 → SOXS (반도체 인버스 ETF)
    "NVDA": "SOXS",
    "AMD": "SOXS",
    "AVGO": "SOXS",
    
    # 디폴트 → SPXS (S&P 500 인버스 ETF)
    # 기타 모든 개별주 숏 신호
}

def route_signal_symbol(original_symbol: str, base_score: float) -> Dict[str, str]:
    """
    개별주 신호를 적절한 실행 심볼로 라우팅
    
    핵심 로직:
    - 양수 신호 (롱): 원래 심볼 그대로 매수
    - 음수 신호 (숏): 해당 인버스 ETF로 변환하여 매수
    """
    if base_score >= 0:  # 롱 신호
        return {
            "exec_symbol": original_symbol, 
            "intent": "enter_long",
            "route_reason": f"{original_symbol}->자체(롱)"
        }
    
    # 숏 신호 → 인버스 ETF 매수로 변환
    exec_symbol = SYMBOL_ROUTING_MAP.get(original_symbol, "SPXS")
    return {
        "exec_symbol": exec_symbol,
        "intent": "enter_inverse", 
        "route_reason": f"{original_symbol}->{exec_symbol}(인버스)"
    }
```

### 실제 파이프라인에서의 적용

```python
def pipeline_e2e(self):
    """E2E 파이프라인에서 라우팅 적용"""
    for signal_event in raw_signals:
        signal_data = json.loads(signal_event["data"])
        original_symbol = signal_data["ticker"]
        base_score = signal_data["base_score"]
        
        # 심볼 라우팅 실행
        routing_result = route_signal_symbol(original_symbol, base_score)
        exec_symbol = routing_result["exec_symbol"]
        
        # 라우팅된 심볼로 거래 실행
        if auto_mode:
            trade = trading_adapter.submit_market_order(
                ticker=exec_symbol,  # 원래 심볼이 아닌 라우팅된 심볼 사용
                side="buy",  # 인버스 ETF는 항상 매수
                quantity=quantity,
                signal_id=signal_event.message_id
            )
```

## 발견된 심각한 논리적 문제점

### 1. 중복 매수 위험 (Critical Issue)

**문제 상황:**
```
시간 10:00 - MSFT 숏 신호 (-0.4) → SQQQ 매수
시간 10:15 - AAPL 숏 신호 (-0.3) → SQQQ 매수 (중복!)
시간 10:30 - TSLA 숏 신호 (-0.5) → SQQQ 매수 (또 중복!)
시간 10:45 - META 숏 신호 (-0.2) → SQQQ 매수 (또또 중복!)
```

**결과:**
- 같은 ETF(SQQQ)를 동일 세션에 4번 매수
- 의도치 않은 레버리지 효과
- 리스크 집중도 급상승

### 2. 투자 논리의 근본적 모순

**핵심 질문:** 
> 개별주 하나의 하락 신호로 전체 지수를 베팅하는 것이 논리적으로 타당한가?

**구체적 문제:**
- MSFT 하락 예상 → NASDAQ 전체(QQQ) 하락 베팅
- 개별주와 지수의 상관관계가 항상 높지 않음
- 한 종목의 부진이 지수 전체 하락을 의미하지 않음

**예시 시나리오:**
```
상황: MSFT 실적 부진으로 -5% 하락
현재 로직: SQQQ 매수 (NASDAQ 전체 하락 베팅)
문제점: NASDAQ의 다른 구성종목들(AAPL, GOOGL, NVDA 등)은 상승할 수 있음
결과: MSFT는 하락했지만 NASDAQ 지수는 상승 → 손실
```

### 3. 리스크 관리 시스템과의 충돌

**기존 GPT-5 리스크 관리:**
- 거래당 0.5% 위험
- 최대 2% 동시 위험  
- 최대 4개 포지션

**라우팅 시스템으로 인한 문제:**
```python
# 시나리오: 4개 개별주에서 동시 숏 신호
signals = [
    {"ticker": "MSFT", "score": -0.4},  # → SQQQ 매수
    {"ticker": "AAPL", "score": -0.3},  # → SQQQ 매수  
    {"ticker": "TSLA", "score": -0.5},  # → SQQQ 매수
    {"ticker": "META", "score": -0.2}   # → SQQQ 매수
]

# 결과: SQQQ에 0.5% × 4 = 2% 집중
# 문제: 분산 효과 완전 상실, 단일 ETF 의존
```

### 4. 체결률 개선 효과 의문

**원래 가설:**
> "개별주 숏 신호를 ETF로 라우팅하면 체결률이 높아질 것"

**실제 문제:**
- 현재 1,503개 신호 → 2건 체결 (0.13% 체결률)
- 근본 원인이 "개별주 vs ETF" 문제가 아닐 가능성
- 시장 시간, 신호 품질, 리스크 관리 등이 실제 원인일 수 있음

## 재검토 요청사항

### 1. 투자 논리 타당성 검증

**질문:** 개별주 숏 신호를 인버스 ETF 매수로 변환하는 것이 투자학적으로 타당한가?

**고려사항:**
- 개별주와 지수의 상관관계 분석
- 섹터 로테이션이 일어날 때의 영향
- 개별주 이벤트(실적, 뉴스)와 지수 움직임의 차이

### 2. 중복 매수 문제 해결방안

**현재 상황:**
- 여러 개별주 → 같은 ETF 중복 매수
- 포지션 사이징이 개별 신호 기준이라 총 노출도 통제 불가

**가능한 해결책:**
- A) 개별주 숏 신호는 처리하지 않기
- B) 집계 후 판단 (여러 신호를 종합하여 하나의 ETF 거래)
- C) ETF별 일일 매수 한도 설정
- D) 이미 해당 ETF 포지션이 있으면 추가 매수 금지

### 3. 체결률 개선을 위한 대안적 접근

**현재 가설 재검토:**
- ETF 라우팅이 정말 체결률을 높이는가?
- 다른 근본적 원인이 있는 것은 아닌가?

**대안적 접근법:**
- 신호 임계값 조정
- 시장 시간 최적화
- 리스크 파라미터 튜닝
- ETF 전용 신호 생성 시스템

### 4. 시스템 통합성 검토

**기존 시스템과의 호환성:**
- GPT-5 리스크 관리 시스템
- 토큰 버킷 및 API 제한
- LLM 게이팅 시스템
- 티어 기반 스케줄링

---

## 🔄 **업데이트: 바스켓 기반 라우팅 시스템으로 전면 개편 (2025-08-19)**

### GPT 분석 결과 및 구현된 해결책

위의 문제점들에 대한 GPT 분석을 통해 **바스켓 기반 라우팅 시스템**으로 전면 개편했습니다.

### 주요 변경사항

#### 1. 바스켓 집계 시스템 구현

```python
# 바스켓 정의
MEGATECH_BASKET = ["AAPL", "MSFT", "AMZN", "META", "GOOGL", "TSLA"]  # → SQQQ
SEMIS_BASKET = ["NVDA", "AMD", "AVGO"]  # → SOXS

def aggregate_basket_signals(signals_window: List[Dict]) -> Dict[str, Dict]:
    """
    바스켓별로 신호 집계 및 조건 검사
    
    조건:
    - 최소 신호 개수: MEGATECH 3개, SEMIS 2개
    - 음수 비율: 60% 이상
    - 평균 스코어: ≤ -0.12
    - 연속성: 2틱 연속 조건 만족
    """
    # 개별주는 증거, 거래는 바스켓(집단) 기준
```

#### 2. ETF 단일 락 및 상충 포지션 방지

```python
def check_etf_single_lock(etf_symbol: str) -> bool:
    """ETF별 90초 TTL 락 체크"""
    
def check_conflicting_positions(target_etf: str) -> bool:
    """상충 포지션 방지 (QQQ ↔ SQQQ)"""
```

#### 3. 리스크 파라미터 조정 (테스트용)

```python
# .env 파일
RISK_PER_TRADE=0.002    # 0.5% → 0.2%
MAX_CONCURRENT_RISK=0.01 # 2% → 1%
```

### 해결된 문제점들

1. **중복 매수 문제**: ETF 단일 락으로 완전 차단
2. **투자 논리 모순**: 바스켓 집계 조건으로 "집단 증거" 기반 거래
3. **리스크 집중**: 상충 포지션 방지 + 낮은 리스크 파라미터
4. **단일 ETF 의존**: 바스켓별 분산 + 조건 미충족시 거래 차단

### 새로운 동작 방식

```
BEFORE (문제): 
MSFT(-0.4) → SQQQ 매수
AAPL(-0.3) → SQQQ 매수 (중복!)
TSLA(-0.5) → SQQQ 매수 (또 중복!)

AFTER (해결):
수집: MSFT(-0.4), AAPL(-0.3), TSLA(-0.5), META(-0.2), GOOGL(-0.1), AMZN(+0.1)
조건검사: 6개 중 5개 음수 (83% > 60% ✓), 평균 -0.18 (< -0.12 ✓), 3개 이상 ✓
결과: 1회 SQQQ 매수 (집단 판단)
락: SQQQ 90초 락 적용 → 추가 매수 차단
```

## 최종 재검토 요청

**남은 의문점:**

1. **바스켓 기반 접근이 투자학적으로 타당한가?**
   - 개별주 집단의 하락 → 지수 하락 베팅의 논리적 타당성
   - 바스켓 구성과 해당 ETF의 상관관계 분석

2. **집계 조건의 적정성**
   - 60% 음수 비율, -0.12 평균 스코어가 적절한가?
   - 2틱 연속 조건의 필요성

3. **리스크 관리와의 정합성**
   - 0.2%/1% 리스크 파라미터가 너무 보수적인가?
   - GPT-5 원래 권장치(0.5%/2%)와의 차이점

4. **실제 성과 예측**
   - 바스켓 기반 시스템의 체결률 개선 가능성
   - 기존 0.13% 체결률 대비 예상 성과

**특히 핵심 질문:**
> "바스켓 집계 방식으로도 여전히 개별주 신호로 지수를 베팅하는 근본적 논리는 동일합니다. 이것이 정말 투자학적으로 타당한 접근인가요?"

---

*이 질문은 실제 구현 완료 후 발견된 논리적 문제점들을 바탕으로 작성되었습니다.*
*현재 시스템은 월요일 라이브 테스트를 앞두고 있어 신속한 검토가 필요합니다.*