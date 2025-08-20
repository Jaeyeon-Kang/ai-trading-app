# 트레이딩 시스템 이슈 분석 보고서
## 2025-08-20 작성

## 📋 문제 상황 요약

### 발생 시점
- **날짜**: 2025-08-19 (월요일)
- **시간**: 미국 정규장 시간 (14:59~17:11 UTC)

### 증상
1. ✅ **알파카 거래는 정상 실행됨**
   - SQQQ 10주 매수 (14:59 UTC)
   - TZA 34주 매수 (15:40, 16:45, 17:11 UTC 분할 매수)
   - 현재 포지션 유지 중

2. ❌ **슬랙 알림 없음**
   - 거래 실행 알림이 슬랙 채널에 오지 않음
   - 일일 리셋 알림도 없음

3. ❌ **로컬 DB 기록 없음**
   - signals 테이블: 0건
   - trades 테이블: 0건
   - orders_paper 테이블: 0건
   - daily_stats 테이블: 0건

4. ❌ **EOD 자동 청산 실패**
   - 15:55 NY 시간 자동 청산이 작동하지 않음
   - 21:44 UTC에 수동 매도 시도 (시장 마감으로 체결 안됨)

## 🔍 근본 원인 분석

### 1. DB 기록 문제 - 확실한 원인
```python
# app/jobs/scheduler.py - pipeline_e2e 함수
trade = trading_adapter.submit_market_order(...)  # 알파카 API 호출
# ❌ DB 저장 로직 없음!

# app/api/main.py - _save_paper_order 함수
def _save_paper_order(order):  # API 엔드포인트에서만 사용
    INSERT INTO orders_paper ...  # 이 함수는 scheduler에서 호출 안됨
```

**원인**: scheduler.py에서 알파카 거래 실행 후 로컬 DB에 저장하는 코드가 없음

### 2. 슬랙 알림 문제 - 추정 원인

#### 코드 구조
```python
# app/jobs/scheduler.py - 라인 2057~2060
if slack_bot:
    slack_message = f"🚀 *신규 진입*..."
    slack_bot.send_message(slack_message)
```

#### 가능한 원인들
1. **slack_bot이 None**
   - trading_components에서 초기화 실패
   - 환경변수 문제 (SLACK_TOKEN, SLACK_CHANNEL_ID)

2. **send_message 실패**
   - 네트워크 오류
   - Slack API 한도 초과
   - 토큰 권한 문제

3. **도커 재시작으로 로그 손실**
   - 21:48 UTC 도커 재시작
   - 14:59~17:11 거래 시점 로그 모두 사라짐

### 3. 워커 크래시 문제 (21:48 이후)
```
ERROR: Process 'ForkPoolWorker' exited with 'signal 9 (SIGKILL)'
```
- 타임아웃으로 워커가 강제 종료됨
- 이로 인해 신호 생성 태스크가 완료되지 못함
- Redis에 새 신호가 추가되지 않아 거래 불가능

## ✅ 정규장 시간에 확인할 사항

### 1. 슬랙봇 초기화 확인
```bash
# 워커 시작 시 슬랙봇 초기화 로그 확인
docker logs trading_bot_worker | grep -E "Slack Bot 초기화|SlackBot 환경변수"
```

### 2. 거래 실행 시 슬랙 메시지 발송 확인
```bash
# 실시간 로그 모니터링
docker logs -f trading_bot_worker | grep -E "slack_bot.send_message|Slack 전송|🚀|📈"
```

### 3. DB 저장 여부 확인
```bash
# 거래 실행 후 DB 확인
docker exec trading_bot_postgres psql -U trading_bot_user -d trading_bot -c "SELECT COUNT(*) FROM trades;"
docker exec trading_bot_postgres psql -U trading_bot_user -d trading_bot -c "SELECT COUNT(*) FROM orders_paper;"
```

### 4. 파이프라인 실행 완료 확인
```bash
# orders_executed 카운트 확인
docker logs trading_bot_worker | grep "orders_executed" | tail -5
```

### 5. 워커 상태 모니터링
```bash
# 워커 크래시 감지
docker logs -f trading_bot_worker | grep -E "SIGKILL|Timed out|ERROR"
```

## 🛠️ 임시 해결 방안

### 1. 슬랙 메시지 수동 확인
```python
# Python 스크립트로 직접 테스트
from app.io.slack_bot import SlackBot
bot = SlackBot()
bot.send_message("테스트 메시지")
```

### 2. 워커 재시작
```bash
docker restart trading_bot_worker
```

### 3. 수동 거래 내역 확인
```bash
# 알파카 API로 직접 확인
curl -H "APCA-API-KEY-ID: ${ALPACA_API_KEY}" \
     -H "APCA-API-SECRET-KEY: ${ALPACA_API_SECRET}" \
     "https://paper-api.alpaca.markets/v2/orders?status=filled&limit=10"
```

## 📊 현재 시스템 상태

### 환경변수 설정
- AUTO_MODE=1 ✅
- BROKER=alpaca_paper ✅
- SLACK_TOKEN=*** (설정됨) ✅
- SLACK_CHANNEL_ID=C099CQP8CJ3 ✅

### 컨테이너 상태 (2025-08-20 00:25 KST 기준)
- trading_bot_worker: Up 3 hours (21:48 재시작)
- trading_bot_api: Up 3 hours
- trading_bot_scheduler: Up 3 hours
- trading_bot_postgres: Up 9 hours
- trading_bot_redis: Up 9 hours

### Redis 신호 상태
- 마지막 신호: 2025-08-19 15:55 UTC
- 재시작 후 새 신호 생성 안됨
- 기존 신호가 `dup_event`로 계속 억제됨

## 🎯 해결 필요 사항

### 단기 (즉시 수정 필요)
1. **DB 저장 로직 추가**
   - scheduler.py의 거래 실행 후 DB INSERT 코드 추가
   - trades, orders_paper 테이블에 기록

2. **슬랙 메시지 에러 로깅**
   - send_message 실패 시 에러 로그 추가
   - slack_bot None 체크 강화

3. **워커 타임아웃 연장**
   - 현재 300초 → 600초로 연장
   - docker-compose.yml 수정

### 중기 (안정성 개선)
1. **거래 기록 일관성 보장**
   - 알파카 거래와 로컬 DB 트랜잭션 처리
   - 실패 시 롤백 메커니즘

2. **모니터링 강화**
   - 거래 실행 시 상세 로깅
   - 슬랙 발송 성공/실패 통계

3. **EOD 청산 로직 검증**
   - 시장 시간 체크 로직 재검토
   - 청산 실행 조건 확인

## 📝 추가 참고사항

### 타임라인
1. 14:59~17:11 UTC: 정상 거래 실행 (알파카 확인)
2. 21:44 UTC: 수동 매도 시도 (시장 마감으로 실패)
3. 21:48 UTC: 도커 재시작
4. 21:48~현재: 워커 크래시 반복, 신호 생성 실패

### 확인된 사실
- 알파카 API 통신은 정상 ✅
- 거래 로직은 정상 작동 ✅
- 리스크 관리 정상 작동 ✅
- 내부 시스템 연동 문제 ❌

---
*이 문서는 2025-08-20 00:30 KST 기준으로 작성되었습니다.*