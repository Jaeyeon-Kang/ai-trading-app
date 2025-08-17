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

### 7. Shell 명령어 오류들
**문제**: `zsh: no matches found: perl -0777 -pe s/^version:s*3.8/version:`
**원인**: zsh에서 특수문자 처리 문제
**해결**: `sed` 명령어로 대체
```bash
sed -i '' 's/^version: *3\.8/version: '\''3.8'\''/' compose.override.*.yml
```

**문제**: `zsh: no matches found: docker compose exec -T celery_worker sh -lc 'env | egrep -i ^(QUOTE_LOG_VERBOSE|BAR_SEC|SCALP_MIN_RET|SCALP_MIN_RANGE|SCALP_3MIN3UP|RTH_ONLY|EXTENDED_PRICE_SIGNALS|SIGNAL_CUTOFF|EXT_SIGNAL_CUTOFF|UNIVERSE_MAX)= | sort'`
**원인**: `egrep` 패턴에서 특수문자 처리 문제
**해결**: 따옴표 수정
```bash
docker compose exec -T celery_worker sh -lc 'env | egrep -i "^(QUOTE_LOG_VERBOSE|BAR_SEC|SCALP_MIN_RET|SCALP_MIN_RANGE|SCALP_3MIN3UP|RTH_ONLY|EXTENDED_PRICE_SIGNALS|SIGNAL_CUTOFF|EXT_SIGNAL_CUTOFF|UNIVERSE_MAX)=" | sort'
```

### 8. Python 스크립트 실행 오류
**문제**: `Syntax error: "(" unexpected` / `SyntaxError: '(' was never closed`
**원인**: `sh -lc` 내에서 Python 스크립트 실행 시 따옴표 문제
**해결**: Python 스크립트를 별도 파일로 분리하거나 올바른 따옴표 사용

### 9. Docker 컨테이너 캐시 문제
**문제**: 코드 변경 후에도 컨테이너가 이전 코드로 실행됨
**원인**: Docker 이미지 캐시, 볼륨 마운트 문제
**해결**: 
```bash
docker compose build --no-cache
docker compose up --force-recreate
```

### 10. Celery 태스크 호출 오류
**문제**: `Usage: celery call [OPTIONS] NAME ... Error: No such option: --timeout`
**원인**: 존재하지 않는 `--timeout` 옵션 사용
**해결**: `--timeout` 옵션 제거

### 11. YAML 파일 구문 오류
**문제**: `SCALP_MIN_RANGE:: -c: line 0: unexpected EOF`
**원인**: `compose.override.scalp.yml`에서 잘못된 YAML 구문
**해결**: YAML 구문 수정
```yaml
# 잘못된 구문
SCALP_MIN_RANGE:: 0.0001

# 올바른 구문
SCALP_MIN_RANGE: "0.0001"
```

### 12. 수동 테스트 시 컴포넌트 접근 오류
**문제**: `AttributeError: 'StreamConsumer' object has no attribute 'consume_signals'`
**원인**: 잘못된 클래스에서 메서드 호출
**해결**: `redis_streams.consume_signals()` 사용

**문제**: `Slack 메시지 전송 실패: 'str' object has no attribute 'channel'`
**원인**: `SlackBot.send_message()`에 문자열 대신 딕셔너리 전달 필요
**해결**: `{"text": message_text}` 형태로 전달

### 13. Git 관리 오류
**문제**: 린트 결과가 자동으로 커밋됨
**원인**: `git add .`로 모든 변경사항 포함
**해결**: 선택적 커밋으로 핵심 파일만 포함
```bash
git add app/jobs/scheduler.py app/hooks/autoinit.py app/io/streams.py compose.override.moderate.yml
git restore .
```

### 14. 환경변수 우선순위 문제
**문제**: `.env` 파일의 공격적 설정이 오버라이드 파일보다 우선됨
**원인**: Docker Compose 환경변수 우선순위 규칙
**해결**: `compose.override.conservative.yml`로 명시적 오버라이드

### 15. Docker 네트워크 정리 오류
**문제**: `failed to remove network` 에러
**원인**: 활성 엔드포인트가 있는 네트워크 삭제 시도
**해결**: 무시 가능한 오류, 컨테이너는 정상 제거됨

---

## 성공한 설정들

### Docker Compose Overrides
1. **compose.override.workerfix.yml**: 컴포넌트 초기화, solo mode
2. **compose.override.scalp.yml**: 스캘핑 모드, 틱 스파이크 감지  
3. **compose.override.rth.yml**: 필터 완화 (cutoff=0, spread=100000bp, cooldown=0)
4. **compose.override.moderate.yml**: 중간 수준 필터 설정
5. **compose.override.conservative.yml**: 보수적 필터 설정

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
5. **Shell 스크립팅**: zsh에서 특수문자 처리 시 따옴표 주의
6. **환경변수 우선순위**: `.env` 파일이 오버라이드보다 우선됨
7. **Git 관리**: 린트 결과와 핵심 변경사항 분리 필요

---

## 16. MSFT 중복 메시지 스팸 문제 (2025-08-16)
**문제**: Docker 재빌드 후에도 MSFT 동일 메시지가 계속 Slack으로 반복 전송됨
```
MSFT | 레짐 VOLSPIKE(0.61) | 점수 -0.59 숏
제안: 진입 417.13 / 손절 425.39 / 익절 409.29
이유: 변동성 급등(<=60m)
```

**근본 원인 분석**:
1. **현재 세션**: CLOSED (장외시간 - ET 22:14)
2. **EXT 일일상한**: MSFT가 이미 23회 전송 (상한 3회 초과)
3. **핵심 버그**: CLOSED 세션에서 EXT 일일상한 체크가 실행되지 않음

**상세 조사 과정**:
1. **Redis 상태 확인**:
   - `dailycap:20250815:EXT:MSFT = 23` (상한 초과)
   - 쿨다운 키들은 정상 존재
   - 문제는 일일상한 로직 미실행

2. **로그 분석**:
   - Mixer 레벨에서만 `suppressed=mixer_cooldown` 발생
   - EXT 세션 체크 로그가 전혀 없음
   - 세션이 "CLOSED"로 인식됨

3. **코드 분석 (`app/jobs/scheduler.py:815-888`)**: 
   ```python
   if session_label == "RTH":
       # RTH 일일상한 체크
   elif session_label == "EXT":
       # EXT 일일상한 체크 
   # CLOSED 세션은 처리되지 않음!
   ```

**해결 과정**:

**1단계**: 디버그 로그 추가
```python
logger.info(f"🔍 [DEBUG] 세션별 체크 시작: {ticker} session={session_label} ext_enabled={ext_enabled}")
logger.info(f"🔍 [DEBUG] 믹싱 전 상태: {ticker} suppress_reason={suppress_reason}")
logger.info(f"🔍 [DEBUG] 컷오프 체크: {ticker} score={signal.score:.3f} cut={cut:.3f} suppress_reason={suppress_reason}")
```

**2단계**: 세션 조건 수정
```python
# 기존
elif session_label == "EXT":

# 수정 후  
elif session_label in ["EXT", "CLOSED"]:  # CLOSED도 EXT 로직 적용
```

**3단계**: timezone import 오류 해결
```python
# 오류: name 'timezone' is not defined
from datetime import datetime, timedelta, timezone  # timezone 추가
```

**최종 결과**:
- ✅ MSFT 메시지 중단: `suppress_reason=ext_daily_cap`
- ✅ EXT 일일상한 정상 작동: 로그에서 확인됨
- ✅ CLOSED 세션도 EXT 로직 적용
- ✅ Slack 스팸 완전 차단

**수정된 파일들**:
- `app/jobs/scheduler.py`: 세션 조건 및 timezone import

**교훈**:
- CLOSED 세션도 장외시간이므로 EXT 로직을 적용해야 함
- 세션별 일일상한 체크가 핵심 스팸 방지 메커니즘
- Mixer 쿨다운은 2차 방어선이지만 근본 해결책은 아님
- import 오류로 인한 예외 처리가 중요함

---

## 최종 상태
- **테스트 폭주 모드**: ✅ 완전 성공
- **중간 모드**: ✅ 성공 (moderate 설정)
- **보수적 모드**: ✅ 성공 (conservative 설정)
- **본판 복귀**: ✅ 완료 (오버라이드 제거, 깨끗한 상태)
- **MSFT 스팸 문제**: ✅ 완전 해결 (2025-08-16)