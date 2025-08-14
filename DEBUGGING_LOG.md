# 테스트 폭주 모드 디버깅 로그

## 🎯 목표
프리장/애프터장 허용, 필터 완화, 유니버스 확장으로 알림이 우수수 터지게 만들기

## 📋 설정된 오버라이드들

### 1. `compose.override.workerfix.yml` - 워커 안정화
```yaml
services:
  celery_worker:
    command: sh -lc 'exec celery -A app.jobs.scheduler worker --loglevel=DEBUG -P solo -c 1 -I app.hooks.autoinit'
    environment:
      LOG_LEVEL: DEBUG
      CELERY_IMPORTS: app.hooks.autoinit
```
**목적**: Solo 모드로 멀티프로세스 문제 해결, autoinit 강제 로드

### 2. `compose.override.scalp.yml` - 스캘핑 모드  
```yaml
services:
  celery_worker:
    environment:
      SCALP_TICK_SPIKE: true
      SCALP_MIN_RET: 0.00005     # 0.005% 변화로 신호 발생
      SCALP_MIN_RANGE: 0.0001    # 0.01% 레인지로 신호 발생
      SCALP_3MIN3UP: true        # 3분봉 3연속 양봉 감지
      QUOTE_LOG_VERBOSE: true    # 야후 API 상세 로그
      BAR_SEC: 15                # 15초봉 사용
```
**목적**: 아주 작은 가격 변화에도 스캘프 신호 생성

### 3. `compose.override.rth.yml` - 필터 완화
```yaml
services:
  celery_worker:
    environment:
      SIGNAL_CUTOFF: "0.0"       # 모든 신호 통과
      EXT_SIGNAL_CUTOFF: "0.0"   # 연장장 신호도 모두 통과
      MAX_SPREAD_BP: "100000"    # 스프레드 제한 사실상 해제
      COOLDOWN_MIN: "0"          # 쿨다운 해제
```
**목적**: 모든 필터링을 무력화해서 신호가 막히지 않게 함

## 🐛 해결한 주요 문제들

### 1. Docker Compose 버전 문제
**에러**: `version must be a string`
**해결**: `version: 3.8` → `version: '3.8'` (따옴표 추가)

### 2. Python 들여쓰기 문제  
**에러**: `IndentationError: unindent does not match any outer indentation level (quotes_delayed.py, line 96)`
**해결**: `app/io/quotes_delayed.py` 96번째 줄 들여쓰기 수정, 컨테이너 이미지 재빌드

### 3. 컴포넌트 초기화 실패
**에러**: `필수 컴포넌트 미준비(quotes_ingestor/regime_detector/signal_mixer/slack_bot)`
**원인**: 멀티프로세스 환경에서 컴포넌트가 각 프로세스마다 따로 초기화됨
**해결**: 
- Solo 모드(`-P solo -c 1`) 적용
- `app/hooks/autoinit.py` 강제 로드
- 필요 시 현장에서 컴포넌트 생성하는 fallback 로직 추가

### 4. 신호 cutoff 필터링 문제
**문제**: 신호 점수가 0.3~0.5인데 기본 cutoff가 0.68이라 모두 걸러짐
**해결**: `SIGNAL_CUTOFF: "0.0"`으로 설정하여 모든 신호 통과

### 5. Slack 메시지 전송 포맷 에러
**에러**: `'str' object has no attribute 'channel'`
**원인**: `SlackBot.send_message()`가 dict 또는 `SlackMessage` 객체를 받는데 문자열 전달
**해결**: `{"text": "메시지"}` 형태의 dict로 전송

### 6. 신호 소비 파이프라인 누락
**문제**: 신호는 Redis Streams에 발행되지만(2312개 쌓임) 소비하는 태스크가 없음
**해결**: 수동 테스트로 `redis_streams.consume_signals()` → Slack 전송 성공 확인

## 📊 현재 상태

### ✅ 성공한 것들
- 스캘프 신호 생성: `스캘프 신호: AAPL short (ret>=0.005%, abs_ret 0.03%)`
- 일반 신호 생성: `시그널 생성: MSFT long (점수: 0.47, 신뢰도: 0.35)`
- Redis Streams 발행: 2312개 신호 누적
- Slack Bot 초기화: `Slack Bot 초기화: 채널 C099CQP8CJ3`
- 수동 신호 소비 및 Slack 전송: `SUCCESS: Signal sent to Slack!`

### 🔄 남은 작업
- [ ] 자동 신호 소비 태스크를 스케줄에 추가
- [ ] 우수수 쏟아지는 알림 확인

## 💡 핵심 명령어들

### 현재 설정으로 실행
```bash
docker compose -f docker-compose.yml -f compose.override.workerfix.yml -f compose.override.scalp.yml -f compose.override.rth.yml up -d
```

### 수동 신호 생성
```bash
docker compose exec -T celery_worker sh -lc 'celery -A app.jobs.scheduler call app.jobs.scheduler.generate_signals'
```

### Redis Streams 신호 확인
```bash
docker compose exec -T redis sh -lc 'redis-cli XLEN signals.raw'
docker compose exec -T redis sh -lc 'redis-cli XREVRANGE signals.raw + - COUNT 3'
```

### 로그 모니터링
```bash
docker compose logs -f celery_worker | grep -E '시그널|스캘프|Slack'
```

## 🎯 성과
- 연장장 활성화 ✅
- 필터 무력화 ✅  
- 스캘프 모드 ✅
- 신호 생성 ✅
- Redis Streams 발행 ✅
- Slack 전송 파이프라인 구축 ✅

**다음**: 자동 소비 태스크 추가로 완전 자동화 달성!
