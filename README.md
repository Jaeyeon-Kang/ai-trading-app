# 미국 주식 자동매매 봇

시드 100만원, 월 LLM 비용 8만원 이하로 운영 가능한 미국 주식 라이트/반자동 트레이딩봇입니다.

## 🎯 주요 특징

- **저비용 운영**: 월 8만원 이하 LLM 비용, 무료 데이터 소스 활용
- **레짐 기반 전략**: Trend/Vol-Spike/Mean-Revert 상황별 동적 가중치
- **EDGAR 오버라이드**: SEC 공시 기반 우선순위 시그널
- **리스크 관리**: VaR95, 일일 손실 한도, 포지션 제한
- **단계별 운영**: 라이트봇 → 반자동 → 완전자동

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

## 🚀 빠른 시작

### 1. 저장소 클론
```bash
git clone <repository-url>
cd ai-trading-app
```

### 2. 환경변수 설정
```bash
cp .env.example .env
# .env 파일을 편집하여 실제 값 입력
```

### 3. Docker로 실행
```bash
docker-compose up -d
```

### 4. 서비스 확인
- API: http://localhost:8000
- Grafana: http://localhost:3000 (admin/admin)
- pgAdmin: http://localhost:8080
- Redis Commander: http://localhost:8081

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
