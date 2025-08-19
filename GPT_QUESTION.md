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

## 요청하는 분석

1. **투자 논리 분석:** 개별주→ETF 라우팅의 이론적 타당성
2. **리스크 평가:** 중복 매수 및 집중 위험의 정량적 평가  
3. **대안 제시:** 더 안전하고 효과적인 접근 방법
4. **구현 방향:** 현재 로직을 어떻게 개선할지

**특히 다음 핵심 질문에 대한 명확한 답변을 요청합니다:**

> "개별주식 하나의 하락 신호로 해당 지수 전체에 베팅하는 것이 논리적으로 타당한가? 그리고 이런 방식이 정말로 체결률을 개선하고 리스크를 줄이는가?"

---

*이 질문은 실제 구현 완료 후 발견된 논리적 문제점들을 바탕으로 작성되었습니다.*
*현재 시스템은 월요일 라이브 테스트를 앞두고 있어 신속한 검토가 필요합니다.*