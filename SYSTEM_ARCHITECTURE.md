# 트레이딩봇 기획안 (라이트봇 → 풀봇)

> 파일명 제안: `TRADINGBOT_SPEC.md`
> 목적: Cursor(로컬 AI)와 사람이 **동일한 기준서**를 보고 개발/QA/운영한다.

---

## TL;DR

* **목표**: 한국에서 소자본(시드 100만 원)으로 **미국 주식/ETF를 공격적 운용**하기 위한 자동화 스택.
* **2단계 전략**

  1. **라이트봇(현재 단계)**: 분석→알림→기록. 거래는 \*\*페이퍼(모의)\*\*만.
  2. **풀봇(다음 단계)**: 반자동→자동 주문(IBKR/KIS), OCO, intraday VaR 가드.
* **핵심 알파**: **레짐 감지 + 기술점수 + EDGAR 감지**, LLM은 **검색 금지**·**해석 전용**, **월 비용 상한 8만 원**.
* **운영 원칙**: 장중(RTH) 위주, **컷오프 0.70**, **EDGAR 중복 차단**, 슬랙 \*\*버튼(매수/패스)\*\*로 **주문 기록**.

---

## 1) 범위와 성공 기준

### 라이트봇(현재 단계)

* **범위**: 실시간/지연 데이터로 신호 생성 → **슬랙 알림**(+ 버튼) → **DB 기록** → **아침 리포트**.
* **성공 기준(DoD)**

  * 슬랙 신호 알림이 \*\*컷오프(≥0.70)\*\*만 도착.
  * 버튼 클릭 시 `/orders/paper` 기록 생성, **EDGAR dedupe** 동작.
  * 매일 **06:10 KST** 리포트 자동 발송(LLM 비용/에러/후보신호 포함).
  * LLM 비용 **0\~소액**(EDGAR/볼스파이크 + RTH 조건에서만 호출).

### 풀봇(다음 단계)

* **범위**: IBKR/KIS 주문 라우팅, OCO, 포지션 관리, intraday VaR 95% 가드.
* **성공 기준**: 반자동/자동 체결, 체결 지연/슬리피지 모니터링, 리스크 규율 준수.

---

## 2) 차별점(=알파 원천)

1. **레짐 감지**: trend / vol\_spike / mean\_revert에 따라 **가중치·진입 기준** 동적으로 조정.
2. \*\*EDGAR 이벤트(8-K, Form 4 등)\*\*를 **강신호 플래그**로 반영.
3. **LLM은 검색 금지, 해석 전용**(헤드라인/공시 스니펫→JSON 요약). **조건부 호출**+월 상한.

---

## 3) 시스템 개요

```
[Stream Collector]  →  [Regime Detector] → [TechScore] → [LLM Insight]
      ↑   EDGAR/X  \                                 /
      └──── quotes  →→→→→→→→→  [Signal Mixer]  → [Alert Dispatcher (Slack)]
                                        ↓
                                    [Orders (paper)] ← Slack buttons
                                        ↓
                                  [Daily Report (06:10)]
```

* **백엔드**: FastAPI(REST), Celery(beat/worker), Redis(Streams), PostgreSQL(DB)
* **데이터**:

  * 시세/호가: 딜레이드(yfinance/IEX 등) → **30초 봉 집계**(OHLCV, 추정 스프레드)
  * 공시: EDGAR 스캐너(초기 모의→추후 실데이터 전환)
* **LLM**: 해석 전용(JSON). **조건**: `edgar_event == True OR regime == "vol_spike"` **AND RTH\_ONLY**
* **알림**: Slack Bot(+ Interactivity, Signing Secret)

---

## 4) 핵심 로직

### 4.1 레짐 감지 (예시)

* **Trend**: EMA20↑/EMA50↑, 이격·기울기·간이 ADX·최근 모멘텀
* **Vol-Spike**: 최근 2분 변동성/거래량이 과거 30분 95퍼센타일 초과 + 거래량 2배↑
* **Mean-Revert**: RSI 극단(≤30/≥70) 이후 **복귀** + 밴드 중앙 회귀

### 4.2 기술점수(TechScore)

* EMA/MACD/RSI/VWAP 편차를 **0\~1 정규화** → `tech_score`.

### 4.3 LLM 해석(JSON 출력)

입력(≤1000자): 헤드라인/공시 스니펫
출력:

```json
{"sentiment": -1.0~1.0, "trigger": "earnings|guidance|form4_buy|...", "horizon_minutes": 30~240, "summary": "한 줄"}
```

### 4.4 시그널 믹서

* 가중치(레짐별):

  * Trend: **tech 0.75 / sent 0.25**
  * Vol-Spike: **tech 0.30 / sent 0.70**
  * Mean-Revert: **tech 0.60 / sent 0.40**
* **EDGAR 보너스**: ±0.1
* **임계치**: 진입 후보 `score ≥ +0.70`(롱) / `≤ -0.70`(숏)  ← 운영 컷오프

### 4.5 리스크 제안(라이트 단계는 “제안”만)

* 기본 **손절 -1.5% / 익절 +3.0%**(레짐 따라 ±0.5% 조정)
* **intraday VaR 95%**(슬라이딩 1만 샘플) 계산 → 리포트 표기

---

## 5) I/O·인터페이스

### 5.1 Redis Streams 키

```
news.edgar, quotes.{TICKER}, signals.raw, signals.tradable,
orders.paper, fills.paper, risk.pnl
```

### 5.2 API 엔드포인트(예)

* `GET /healthz` – 내부 의존성 무관 헬스
* `GET /report/daily?force=1&post=1` – 아침 리포트 생성(+슬랙 전송)
* `GET /signals/recent?hours=24` – 최근 후보 신호 목록(KST)
* `POST /slack/interactions` – 버튼 콜백(서명 검증)

### 5.3 Slack 알림 포맷(예)

```
AAPL | 레짐 TREND(0.80) | 점수 +0.72
제안: 진입 224.5 / 손절 221.1 / 익절 231.2
이유: 가이던스 상향(<=120m)
[✅ 매수] [❌ 패스]
```

---

## 6) 데이터베이스(요약 스키마)

* **signals**(id, ts\_utc, ts\_kst, ticker, regime, tech\_score, sent\_score, final\_score, reason)
* **orders\_paper**(id, ts, ticker, side, qty, px\_entry, score, regime, reason, source)
* **fills\_paper**(id, order\_id, ts, px\_fill, side, reason)
* **edgar\_events**(id, ts, ticker, form, url, snippet\_hash, snippet\_text, deduped\_at)
* **risk\_metrics**(id, ts, var95, pnl\_intraday, latency\_p99)

---

## 7) 환경변수(.env) 규격

필수:

```
REDIS_URL=redis://redis:6379/0
POSTGRES_URL=postgresql://trading_bot_user:trading_bot_password@postgres:5432/trading_bot
API_BASE_URL=http://trading_bot_api:8000

SLACK_BOT_TOKEN=xoxb-...
SLACK_SIGNING_SECRET=...
SLACK_CHANNEL_ID=CXXXXXXXX

OPENAI_API_KEY=sk-...
LLM_ENABLED=true
LLM_MONTHLY_CAP_KRW=80000
RTH_ONLY=true
DEFAULT_QTY=1
TZ=Asia/Seoul
```

주의:

* **채널 “이름” 금지**, **ID 사용**(예: `C0123456789`)
* `SLACK_CHANNEL`는 사용하지 않음
* `RTH_ONLY=true` 기본값 권장

---

## 8) 비용·운영 정책

* **LLM 비용 상한**: 월 **8만 원** 하드캡
* 호출 조건: `(edgar_event OR regime=='vol_spike') AND RTH_ONLY`
* 비용 집계: LLM 호출 래퍼에서 **Redis 누적**(`metrics:llm:{YYYYMMDD}`) → 리포트에서 취합
* **EDGAR dedupe**: `md5(summary|url)` → Redis Set(`edgar:dedupe:snippets`) + DB `ON CONFLICT`.

---

## 9) 배포/실행

### 로컬(Docker)

```
docker compose up -d
curl -s http://localhost:8000/healthz
curl -s "http://localhost:8000/report/daily?force=1&post=1"
```

* Slack Interactivity(실 콜백) 필요 시, **ngrok/cloudflared**로 터널 → Request URL: `https://<public>/slack/interactions`

### PaaS(추천)

* **Redis**: Upstash, **Postgres**: Neon, **App**: Render
* 환경변수는 PaaS 대시보드에 등록, **Request URL** 고정

---

## 10) QA 시나리오(라이트봇 Day1\~Day2)

* **A 헬스/리포트**: `/healthz` healthy, `/report/daily?force=1` success
* **B 슬랙 인터랙션**: 모의 서명 POST → **HTTP 200**, `orders_paper` 생성
* **C EDGAR dedupe**: 동일 해시 2회 push → **DB count=1**
* **D LLM 비용 가드**: 리포트의 `llm_cost_krw` 저비용, 트리거는 **edgar/vol\_spike**만
* **E recent 신호**: 컷오프 이상만 노출, 필드(kst/score/티커/레짐/이유) OK
* **F 실버튼**: 채널에서 ✅ 매수 → `orders_paper` 신규행 생성

---

## 11) Day3(다음 단계) 사양 — **성과 리포트 자동화**

* **시뮬레이터**: `orders_paper` → 캔들 스캔 → **OCO(손절/익절)** 먼저 닿은 쪽으로 `fills_paper` 생성
* **지표**: 승률, 평균 이익/손실, **PF**, **PnL**, **Avg Hold**, **슬리피지(bp)**
* **분해**: **레짐/티커별 성과**
* **엔드포인트**: `GET /report/perf?days=1` (06:10 리포트에 성과 블록 병합)
* **품질 로그**: 미체결/데이터누락 카운트

---

## 12) 보안·컴플라이언스

* Slack Signing Secret 필수(유출 시 재발급)
* SEC EDGAR 호출 주기/UA 준수(실데이터 전환 시)
* 키는 `.env`/PaaS Secret에만 저장, 저장소 커밋 금지

---

## 13) 운영 런북(요약)

* **시간대**: 내부 **UTC**, 리포트 표기는 **KST**
* **컷오프 운영값**: 0.70(라이트봇), 풀봇 시 내부 체결컷 별도
* **에러 대응**: Slack 실패 시 재시도/백오프, Worker readiness gate 통과 확인
* **버전**: 백테스트/실거래 **동일 이미지 태그** 원칙

---
