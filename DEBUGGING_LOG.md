# AI Trading Bot 테스트 폭주 모드 디버깅 로그

## 프로젝트 개요
- **목표**: 테스트 폭주 모드로 신호를 대량 생성하고 Slack으로 자동 전송
- **날짜**: 2025-08-15
- **최종 결과**: ✅ 성공

---

## 주요 문제들과 해결 과정

### 1. Docker Compose 설정 오류
**문제**: `version must be a string`
```yaml
# 잘못된 설정
version: 3.8

# 올바른 설정  
version: '3.8'
```
**해결**: 모든 compose override 파일에서 version을 문자열로 수정

### 2. 컴포넌트 초기화 실패
**문제**: `필수 컴포넌트 미준비` 에러로 신호 생성 불가
**원인**: Celery worker에서 trading_components가 제대로 초기화되지 않음
**해결**:
- `app/hooks/autoinit.py` 생성하여 자동 초기화 로직 구현
- `compose.override.workerfix.yml`에서 `CELERY_IMPORTS` 설정
- Worker 명령어에 `-P solo -c 1 -I app.hooks.autoinit` 추가

### 3. 코드 문법 오류들
**문제**: `AttributeError: 'Candle' object has no attribute 'close'`
**해결**: `candles[-1].close` → `candles[-1].c`로 수정

**문제**: 여러 `IndentationError`들 
**해결**: `app/io/quotes_delayed.py`에서 tab/space 혼용 문제 수정

### 4. Redis Streams 타입 오류
**문제**: `Invalid input of type: 'int64'. Convert to a bytes, string, int or float first.`
**해결**: `app/io/streams.py`에 `_coerce_message_fields` 함수 추가하여 numpy 타입 변환

### 5. 신호 소비 태스크 미실행
**문제**: `consume_signals` 태스크가 Celery Beat에서 실행되지 않음
**해결**: 신호 소비 로직을 `generate_signals` 태스크 내부로 이동하여 즉시 처리

### 6. Slack 전송 포맷 오류 (최종 문제)
**문제**: `Unknown format code 'f' for object of type 'str'`
**원인**: Redis에서 가져온 score 값이 문자열 타입
**해결**:
```python
# 타입 처리 (numpy, string 등)
try:
    if hasattr(score, 'item'):
        score = score.item()
    score = float(score)
except:
    score = 0.0
```

---

## 성공한 설정들

### Docker Compose Overrides
1. **compose.override.workerfix.yml**: 컴포넌트 초기화, solo mode
2. **compose.override.scalp.yml**: 스캘핑 모드, 틱 스파이크 감지  
3. **compose.override.rth.yml**: 필터 완화 (cutoff=0, spread=100000bp, cooldown=0)

### 핵심 환경변수
```bash
RTH_ONLY=false
EXTENDED_PRICE_SIGNALS=true
SCALP_TICK_SPIKE=true
SCALP_3MIN3UP=true
SIGNAL_CUTOFF=0.0
EXT_SIGNAL_CUTOFF=0.0
MAX_SPREAD_BP=100000
COOLDOWN_MIN=0
UNIVERSE_MAX=120
```

### 최종 신호 플로우
1. `generate_signals` 태스크에서 49개 신호 생성 (23초)
2. Redis Streams에 신호 발행
3. 동일 태스크 내에서 즉시 10개씩 소비
4. 각 신호를 Slack으로 전송
5. 성공 로그: `🎉 신호 소비 완료: 10개 Slack 전송`

---

## 성공 지표
- ✅ 신호 생성: 49개/실행
- ✅ 신호 소비: 10개/배치
- ✅ Slack 전송: 100% 성공률
- ✅ 대상 종목: AAPL, MSFT, TSLA, NVDA, AMZN, ARM, PDD, TRIB, HOOD, GME 등

---

## 교훈
1. **타입 안전성**: Redis Streams 데이터는 항상 문자열로 저장됨 
2. **컴포넌트 초기화**: Celery worker에서 trading_components 초기화가 핵심
3. **태스크 스케줄링**: 복잡한 inter-task 통신보다 단일 태스크 내 처리가 안정적
4. **볼륨 마운트**: 코드 변경 후 반드시 Docker 이미지 리빌드 필요

---

## 최종 상태
- **테스트 폭주 모드**: ✅ 완전 성공
- **다음 단계**: 오버라이드 제거하고 본판 코드로 복귀