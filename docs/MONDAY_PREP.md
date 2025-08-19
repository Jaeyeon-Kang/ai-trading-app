# 🔄 월요일 테스트 전 시스템 초기화 가이드

> **목표**: 깨끗한 상태에서 바스켓 기반 라우팅 시스템 테스트 시작  
> **실행 시점**: 월요일 한국시간 21:30 (미국 정규장 시작)  
> **테스트 중점**: 바스켓 집계 로직, ETF 단일 락, 상충 포지션 방지

---

## 🧹 1단계: 알파카 페이퍼 계좌 정리

### 모든 주문 취소
```bash
# 알파카 페이퍼 계좌의 모든 활성 주문 취소
curl -X GET "https://paper-api.alpaca.markets/v2/orders" \
  -H "APCA-API-KEY-ID: $ALPACA_API_KEY" \
  -H "APCA-API-SECRET-KEY: $ALPACA_SECRET_KEY"

# 활성 주문이 있다면 하나씩 취소
curl -X DELETE "https://paper-api.alpaca.markets/v2/orders/{order_id}" \
  -H "APCA-API-KEY-ID: $ALPACA_API_KEY" \
  -H "APCA-API-SECRET-KEY: $ALPACA_SECRET_KEY"
```

### 모든 포지션 정리
```bash
# 현재 포지션 확인
curl -X GET "https://paper-api.alpaca.markets/v2/positions" \
  -H "APCA-API-KEY-ID: $ALPACA_API_KEY" \
  -H "APCA-API-SECRET-KEY: $ALPACA_SECRET_KEY"

# 포지션이 있다면 청산 (자동으로 처리됨 - 페이퍼 계좌)
```

---

## 🗂️ 2단계: Redis 데이터 초기화

### Redis 캐시 정리
```bash
# Docker를 통한 Redis 클리어
docker exec trading_bot_redis redis-cli FLUSHALL

# 또는 선택적 정리
docker exec trading_bot_redis redis-cli DEL "signals:*"
docker exec trading_bot_redis redis-cli DEL "portfolio:*"
docker exec trading_bot_redis redis-cli DEL "risk:*"
```

---

## 📊 3단계: 데이터베이스 정리 (선택사항)

### 테스트 데이터만 정리
```sql
-- 기존 테스트 거래 기록 정리 (프로덕션에서는 주의)
-- DELETE FROM trades WHERE created_at >= '2025-08-17';
-- DELETE FROM signals WHERE created_at >= '2025-08-17';

-- 또는 새로운 세션으로 마킹
UPDATE portfolio_snapshots SET is_test = true 
WHERE created_at >= '2025-08-17';
```

---

## 🔧 4단계: Docker 컨테이너 클린 재시작

### 전체 시스템 재시작
```bash
# 1. 현재 컨테이너 정리
docker compose down

# 2. 로그 정리 (선택사항)
docker system prune -f

# 3. 이미지 재빌드 (최신 코드 반영)
docker compose build --no-cache

# 4. 클린 상태로 재시작
docker compose up -d

# 5. 상태 확인
docker compose ps
```

---

## ✅ 5단계: 시스템 검증

### 초기화 확인 체크리스트
```bash
# 1. 포트폴리오 API 초기 상태 확인
curl -s http://localhost:8000/portfolio/summary | python -m json.tool

# 기대값:
# {
#   "data": {
#     "cash": 100000.0,
#     "positions_count": 0,
#     "risk_metrics": {
#       "current_risk_pct": 0,
#       "risk_status": "safe"
#     }
#   }
# }

# 2. Redis 초기화 확인
docker exec trading_bot_redis redis-cli KEYS "*"
# 결과: (empty list) 또는 시스템 기본 키만

# 3. 컨테이너 상태 확인
docker compose ps
# 모든 컨테이너 running (healthy) 상태

# 4. 로그 확인
docker logs trading_bot_api --tail 10
docker logs trading_bot_scheduler --tail 10
docker logs trading_bot_worker --tail 10
```

---

## 🎯 6단계: 테스트 설정 최종 확인

### 환경 변수 점검
```bash
# .env 파일 확인
BROKER=alpaca_paper      # 또는 kis, paper(내장 시뮬레이터)
AUTO_MODE=1              # 완전 자동 모드 🤖
SEMI_AUTO_BUTTONS=1      # 반자동 버튼 활성화(선택)
LLM_GATING_ENABLED=true  # LLM 게이트 활성화
LLM_MIN_SIGNAL_SCORE=0.6 # 테스트 가속 시 임시 완화(기본 0.7)
RTH_DAILY_CAP=100        # 테스트 가속용 상향(기본 5)
```

### 리스크 설정 확인
```bash
# 리스크 매니저 설정 최종 점검
docker exec trading_bot_api python -c "
from app.engine.risk_manager import get_risk_manager
rm = get_risk_manager()
print('✅ 거래당 위험:', f'{rm.config.risk_per_trade:.1%}')
print('✅ 동시위험 한도:', f'{rm.config.max_concurrent_risk:.1%}')
print('✅ 일일 손실 한도:', f'{rm.config.daily_loss_limit:.1%}')
print('✅ 최대 포지션:', rm.config.max_positions)
"
```

---

## 🚨 초기화 시 주의사항

### ⚠️ 절대 하지 말 것
```bash
❌ 실거래 계좌 건드리기
❌ 프로덕션 데이터베이스 DROP
❌ .env 파일의 API 키 삭제
❌ 실제 자금이 있는 계좌 조작
```

### ✅ 안전한 초기화 범위
```bash
✅ 알파카 페이퍼 계좌만 정리
✅ Redis 캐시 데이터만 삭제
✅ Docker 컨테이너 재시작
✅ 테스트 로그 정리
✅ 시스템 상태 검증
```

---

## 📋 월요일 17:00 체크리스트

```bash
[ ] 알파카 페이퍼 계좌 포지션 = 0개
[ ] 알파카 페이퍼 계좌 주문 = 0개  
[ ] 포트폴리오 현금 = $100,000
[ ] Redis 신호 데이터 = 클리어
[ ] Docker 컨테이너 = 모두 healthy
[ ] 리스크 설정 = GPT-5 권장값
[ ] Slack 알림 = 정상 작동
[ ] 환경 변수 = 테스트 모드
```

---

## 🤖 완전 자동화 모드 설정

### AUTO_MODE=1 동작 방식
```
🔄 자동 처리 흐름:
1. 신호 생성 (GPT-5 리스크 체크 통과)
2. 자동 주문 실행 (Paper/Alpaca/KIS 설정에 따라)
3. 리스크 기반 수량 자동 계산
4. Slack 알림 (결과 보고용)

🛡️ 안전장치:
- 0.5% 거래당 위험 자동 적용
- 2% 동시위험 한도 자동 차단
- 일일 손실 3% 기본값(권장 2%) 도달 시 자동 중지
- 모든 거래 사전 리스크 검증
```

### 📱 월요일 출근 후 모니터링
```
✅ 해야 할 일:
- Slack 알림으로 거래 결과 확인
- 포트폴리오 성과 주기적 체크
- 큰 손실시 수동 개입 준비

❌ 할 필요 없는 일:
- 버튼 클릭 (완전 자동)
- 수량 계산 (자동 계산)
- 리스크 체크 (자동 처리)
```

---

**실행 예상 시간**: 10-15분  
**실행 담당**: 사용자 초기화 → 시스템 완전 자동 거래  
**검증 필수**: 모든 체크리스트 ✅ 완료 후 테스트 시작  

---

*완전 자동화로 GPT-5 리스크 관리 시스템이 스스로 안전하고 수익성 있는 거래를 수행합니다.*
