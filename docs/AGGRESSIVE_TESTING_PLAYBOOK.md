# 🚀 Aggressive Testing Playbook (Day Trading / Scalping)

> 테스트 세션 동안 보수적 제약을 최소화하고 신호·체결 빈도를 높이는 운용 가이드. 프로덕션 이전에만 사용 권장.

---

## 목표
- 강추세 돌파, 볼륨 스파이크 모멘텀, VWAP 평균회귀를 30초봉 기준으로 빠르게 집행
- EOD 전량 청산 유지, 방향 락/쿨다운 짧게
- 리스크 캡 상향: 거래당 0.8%, 동시위험 4% (테스트 한정)

## 권장 .env (공격 모드)
중복 키는 한 번만 남기고 아래 값으로 통일. 키/시크릿은 그대로 유지.

```env
# thresholds (단일 소스 정합)
MIXER_THRESHOLD=0.10
SIGNAL_CUTOFF_RTH=0.10
SIGNAL_CUTOFF_RTH_DELTA=0.0

# risk (테스트 한정 상향)
RISK_PER_TRADE=0.008
MAX_CONCURRENT_RISK=0.04

# session / execution
RTH_ONLY=false
ENABLE_TRAIL_STOP=1
TRAIL_RET_PCT=0.004
COOLDOWN_SECONDS=120
DIRECTION_LOCK_SECONDS=120

# quotes (스캘핑이면 실시간 권장)
QUOTES_PROVIDER=alpaca

# inverse/leveraged 일관화
INVERSE_ETFS=SOXS,SQQQ,SPXS,TZA,SDOW,TECS,DRV,SARK,UVXY
LEVERAGED_ETFS=SOXS,SQQQ,SPXS,TZA,SDOW,TECS,DRV,UVXY

# cleanup 단일화 예시 (중복 제거 후 아래만 유지)
FRACTIONAL_ENABLED=true
MAX_PRICE_PER_SHARE_USD=120
```

주의:
- 지연 시세로 스캘핑은 체결 불일치가 큼. 꼭 써야 한다면 AUTO_MODE=0 섀도런 먼저.
- Slack 채널 변수 이름을 서비스 전체에서 통일(`SLACK_CHANNEL_ID`).

---

## 코드 정합성 개선(짧은 편집 목록)
- LLM 강신호 임계 환경연동: `app/engine/llm_insight.py`
  - 상단 추가: `from app.config import settings`
  - 변경: `if abs(signal_strength) >= settings.LLM_MIN_SIGNAL_SCORE:` (기존 0.70 하드코드 제거)
- 믹서 임계 단일 소스화: `app/hooks/autoinit.py`
  - 생성부: `buy_threshold=float(os.getenv("BUY_THRESHOLD", str(mixer_thr)))`
  - 생성부: `sell_threshold=float(os.getenv("SELL_THRESHOLD", str(-mixer_thr)))`
- Slack env 통일: `docker-compose.yml`
  - 모든 서비스에 `SLACK_BOT_TOKEN`, `SLACK_CHANNEL_ID` 주입 (`SLACK_CHANNEL` 사용처는 교체)
- UVXY 레버리지 보호: env에 포함되면 `risk_manager`의 레버리지 축소 로직 자동 적용

---

## Claude Cursor 전달용 프롬프트 (한 번에 한 개씩 실행)

### 1) .env 정리(공격 모드 + 중복 제거)
```
아래 키만 남기고 값은 그대로 덮어써서 .env를 정리해줘.
- MIXER_THRESHOLD=0.10
- SIGNAL_CUTOFF_RTH=0.10
- SIGNAL_CUTOFF_RTH_DELTA=0.0
- RISK_PER_TRADE=0.008
- MAX_CONCURRENT_RISK=0.04
- RTH_ONLY=false
- ENABLE_TRAIL_STOP=1
- TRAIL_RET_PCT=0.004
- COOLDOWN_SECONDS=120
- DIRECTION_LOCK_SECONDS=120
- QUOTES_PROVIDER=alpaca
- INVERSE_ETFS=SOXS,SQQQ,SPXS,TZA,SDOW,TECS,DRV,SARK,UVXY
- LEVERAGED_ETFS=SOXS,SQQQ,SPXS,TZA,SDOW,TECS,DRV,UVXY
- FRACTIONAL_ENABLED=true
- MAX_PRICE_PER_SHARE_USD=120
중복 선언된 키(RISK_PER_TRADE, MAX_CONCURRENT_RISK, FRACTIONAL_ENABLED, MAX_PRICE_PER_SHARE_USD, DIRECTION_LOCK_SECONDS 등)는 마지막 값만 남기고 모두 제거.
```

### 2) LLM 임계 환경연동 (`app/engine/llm_insight.py`)
```
다음 변경을 적용해줘.
- 상단에 `from app.config import settings` 추가
- 조건문을 `if abs(signal_strength) >= settings.LLM_MIN_SIGNAL_SCORE:`로 교체
기존 0.70 하드코드 제거. 나머지 로직/로그는 유지.
```

### 3) 믹서 임계 통일 (`app/hooks/autoinit.py`)
```
SignalMixer 생성부를 다음과 같이 바꿔줘.
- buy_threshold=float(os.getenv("BUY_THRESHOLD", str(mixer_thr)))
- sell_threshold=float(os.getenv("SELL_THRESHOLD", str(-mixer_thr)))
로그에도 BUY/SELL 임계값을 함께 출력.
```

### 4) Slack 변수 통일 (`docker-compose.yml`)
```
api/worker/scheduler 세 서비스 모두 환경변수에 다음 두 항목 존재하도록 수정해줘.
- SLACK_BOT_TOKEN=${SLACK_BOT_TOKEN}
- SLACK_CHANNEL_ID=${SLACK_CHANNEL_ID}
기존 SLACK_CHANNEL 사용처가 있으면 SLACK_CHANNEL_ID로 교체.
```

### 5) 실시간 시세 사용 확인
```
QUOTES_PROVIDER가 alpaca일 때 update_quotes 파이프라인이 실제 시세로 동작하는지 확인하고,
지연 소스가 참조되면 실시간 경로로 전환해줘. (필요 시 간단한 플래그 분기)
```

### 6) 재빌드/검증
```
- docker compose build --no-cache && docker compose up -d
- /health, /status 확인
- 정규장 30분 동안 로그에서 actionable/suppressed 분포, 체결/청산 로그 확인
```

---

## 테스트 체크리스트
- actionable rate 상승, flip-flop ≤ 25%, stopout rate ≤ 40%
- MAE/MFE 중앙값 개선, EOD 전량 청산 로그 확인
- LLM 호출: 강신호/스파이크에서만 증가, 월 한도 내

## 롤백 계획
- `RISK_PER_TRADE=0.005`, `MAX_CONCURRENT_RISK=0.02`, `MIXER_THRESHOLD=0.12`, `SIGNAL_CUTOFF_RTH=0.12`
- `COOLDOWN_SECONDS=180`, `DIRECTION_LOCK_SECONDS=180`, `ENABLE_TRAIL_STOP=0`
- QUOTES_PROVIDER=delayed 시 AUTO_MODE=0로 섀도런 전환

---

의심 로그가 보이면 타임스탬프와 함께 슬랙 캡처 남겨두세요. 다음 튠업에 반영합니다.
