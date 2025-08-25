# 🤖 GPT-5 AI 알고리즘 트레이딩 시스템

> **GPT-5 Deep Reasoning 기반 완전 자동화 AI 트레이딩 플랫폼**  
> **수학적으로 검증된 0.5% 거래당 위험, 2% 동시위험 한도**

**Kelly Criterion 분석과 몬테카를로 시뮬레이션을 바탕으로 설계된 차세대 AI 트레이딩 시스템입니다.**

## 🎯 GPT-5 핵심 특징

- **🧮 수학적 정확성**: Kelly Criterion 34.3% 대비 68.6배 보수적 안전 마진
- **🛡️ 리스크 관리**: 0.5% 거래당 위험, 2% 동시위험 한도 (GPT-5 권장)
- **🤖 완전 자동화**: 신호 생성 → 리스크 체크 → 자동 거래 → 실시간 모니터링
- **📊 실시간 AI 분석**: 30초봉 기반 Claude LLM + 기술적 분석 융합
- **⚡ 마이크로서비스**: Docker 기반 확장 가능한 아키텍처
- **🔄 프리/애프터마켓**: 24시간 거래 지원 (04:00-20:00 ET)

## 🏗️ 아키텍처

```
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   Data Sources  │    │   Engine Layer  │    │  Execution      │
│                 │    │                 │    │                 │
│ • Yahoo Finance │───▶│ • Regime Detect │───▶│ • KIS API       │
│ • SEC EDGAR     │    │ • Tech Score    │    │ • Paper Trading │
│ • News APIs     │    │ • LLM Insight   │    │ • Risk Engine   │
└─────────────────┘    │ • Signal Mixer  │    └─────────────────┘
                       └─────────────────┘
                                │
                       ┌─────────────────┐
                       │   Monitoring    │
                       │                 │
                       │ • Slack Bot     │
                       │ • Grafana       │
                       │ • Redis Streams │
                       └─────────────────┘
```

## 📋 요구사항

- Python 3.9+
- Docker & Docker Compose
- KIS 증권 계좌 (미국 주식 거래 가능)
- OpenAI API 키
- Slack Bot Token

## 📚 문서 (docs/ 폴더)

| 문서 | 설명 | 용도 |
|------|------|------|
| **[시스템 아키텍처](docs/SYSTEM_ARCHITECTURE.md)** | 전체 시스템 동작 원리 | 시스템 이해 |
| **[GPT-5 전략](docs/FINAL_TRADING_STRATEGY.md)** | GPT-5 수학적 분석 결과 | 전략 이해 |
| **[구현 로그](docs/IMPLEMENTATION_LOG.md)** | 개발 과정 상세 기록 | 개발 참고 |
| **[월요일 준비](docs/MONDAY_PREP.md)** | 실전 테스트 가이드 | 운영 가이드 |
| **[Aggressive Testing Playbook](docs/AGGRESSIVE_TESTING_PLAYBOOK.md)** | 스캘핑/공격 모드 .env/프롬프트 | 테스트 튜닝 |

## 🚀 빠른 시작

### 1. 저장소 클론 및 초기화
```bash
git clone <repository-url>
cd ai-trading-app

# 월요일 실전 테스트 전 초기화 (권장)
# docs/MONDAY_PREP.md 가이드 참조
```

### 2. 환경변수 설정 (AUTO_MODE=1 권장)
```bash
cp .env.example .env
# 완전 자동화: AUTO_MODE=1
# 알파카 페이퍼: BROKER=alpaca_paper (첫 테스트)
# 공격 모드 튜닝: docs/AGGRESSIVE_TESTING_PLAYBOOK.md 참고 (MIXER_THRESHOLD/SIGNAL_CUTOFF/RISK 상향)
```

### 3. GPT-5 리스크 시스템 가동
```bash
docker compose down
docker compose build --no-cache
docker compose up -d
```

### 4. 실시간 모니터링
```bash
# 포트폴리오 리스크 확인
curl http://localhost:8000/portfolio/summary | jq .data.risk_metrics

# 실시간 로그 모니터링
docker logs -f trading_bot_scheduler | grep "신호\|거래\|위험"
```

## ⚙️ 설정

### 브로커 설정
```bash
# KIS 증권 (권장)
BROKER=kis
KIS_API_KEY=your_api_key
KIS_API_SECRET=your_api_secret

# Alpaca 페이퍼 트레이딩
BROKER=alpaca_paper
ALPACA_API_KEY=your_api_key
ALPACA_API_SECRET=your_secret
```

### 거래 설정
```bash
# 초기 자본
INITIAL_CAPITAL=1000000

# 일일 손실 한도 (3%)
DAILY_LOSS_LIMIT=0.03

# 관심 종목
WATCHLIST=AAPL,MSFT,GOOGL,AMZN,TSLA,NVDA,META,NFLX,AMD,INTC
```

### LLM 설정
```bash
# OpenAI API
OPENAI_API_KEY=your_openai_api_key

# 월 비용 상한 (8만원)
LLM_MONTHLY_CAP_KRW=80000
```

## 📊 운영 단계

### 1단계: 라이트봇 (1-2주)
- 시그널 생성 및 Slack 알림
- 거래 없이 모니터링만
- 시스템 안정성 확인

### 2단계: 반자동 (3-4주)
- Slack 버튼으로 승인/거부
- 작은 금액으로 페이퍼 트레이딩
- 성과 측정 및 조정

### 3단계: 완전자동 (5주차~)
- KIS 실거래 전환
- 자동 주문 실행
- 리스크 관리 강화

## 🔧 API 엔드포인트

### 헬스 체크
```bash
GET /health
```

### 시그널 제출
```bash
POST /signal
{
  "ticker": "AAPL",
  "signal_type": "buy",
  "score": 0.75,
  "confidence": 0.8,
  "trigger": "추세 돌파",
  "summary": "EMA 상향 돌파로 매수 신호",
  "entry_price": 150.25,
  "stop_loss": 148.00,
  "take_profit": 153.50
}
```

### 일일 리포트
```bash
POST /report
{
  "date": "2025-01-01",
  "include_risk": true,
  "include_llm": true
}
```

### 긴급 중지
```bash
POST /emergency-stop
```

## 📈 모니터링

### Grafana 대시보드
- 실시간 시그널 현황
- 거래 성과 지표
- 리스크 지표
- 시스템 상태

### Slack 알림
- 거래 시그널 (승인 버튼 포함)
- 리스크 경고
- 일일 리포트
- 시스템 상태

## 🛡️ 리스크 관리

### 일일 손실 한도
- 기본값: 3% (설정 가능)
- 도달 시 자동 거래 중지

### VaR 기반 관리
- 95% 신뢰수준 VaR 계산
- 임계값 초과 시 경고

### 포지션 제한
- 최대 5개 포지션
- 단일 거래 최대 5% 자본

## 💰 비용 구조

### 월 고정비
- LLM API: ~6-8만원
- 서버/클라우드: 0-2만원 (프리티어 활용)
- **총 비용: 8만원 이하**

### 수익 목표
- 월 6-10% 수익 (공격적)
- 최대 드로다운 -10% 이내

## 🔍 시그널 전략

### 1. 추세 돌파 (Trend Breakout)
- 30초봉 전고 상향 돌파
- VWAP 상단 + ADX 상승
- 가중치: 기술 75% / 감성 25%

### 2. 변동성 급등 (Vol-Spike)
- 거래량 급증 + 가격 변동성
- 뉴스/공시 기반
- 가중치: 기술 30% / 감성 70%

### 3. 평균회귀 (Mean-Revert)
- RSI 극단값에서 반전
- 볼린저 밴드 복귀
- 가중치: 기술 60% / 감성 40%

### EDGAR 오버라이드
- 8-K 실적 발표: +0.1 보너스
- Form 4 임원 매매: 우선순위
- 중요 공시 시 기술신호 덮어쓰기

## 🐛 문제 해결

### 일반적인 문제들

#### Redis 연결 실패
```bash
# Redis 상태 확인
docker-compose logs redis

# Redis 재시작
docker-compose restart redis
```

#### PostgreSQL 연결 실패
```bash
# PostgreSQL 상태 확인
docker-compose logs postgres

# 데이터베이스 재시작
docker-compose restart postgres
```

#### LLM API 제한
```bash
# LLM 상태 확인
curl http://localhost:8000/status

# 비용 상한 확인
# .env 파일에서 LLM_MONTHLY_CAP_KRW 값 조정
```

#### 시그널 생성 안됨
```bash
# 스케줄러 로그 확인
docker-compose logs celery_beat

# 워커 로그 확인
docker-compose logs celery_worker
```

## 📚 개발 가이드

### 새로운 시그널 전략 추가
1. `app/engine/` 디렉토리에 새 엔진 추가
2. `app/engine/mixer.py`에서 가중치 설정
3. `app/jobs/scheduler.py`에서 호출

### 새로운 데이터 소스 추가
1. `app/data/` 디렉토리에 새 인제스터 추가
2. `app/io/streams.py`에서 스트림 정의
3. 스케줄러에서 호출 추가

### 모니터링 지표 추가
1. `app/db/schema.sql`에 테이블 추가
2. Grafana 대시보드에 패널 추가
3. Slack 알림에 포함

## 📄 라이선스

MIT License

## ⚠️ 면책 조항

이 프로젝트는 교육 및 연구 목적으로 제작되었습니다. 실제 거래에 사용할 경우 발생하는 손실에 대해 개발자는 책임지지 않습니다. 투자는 본인의 판단과 책임 하에 진행하시기 바랍니다.

## 🤝 기여하기

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add some amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## 📞 지원

- 이슈: GitHub Issues
- 이메일: support@tradingbot.com
- 문서: [Wiki](https://github.com/username/trading-bot/wiki)
