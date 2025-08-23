# app/config.py
import os
import redis

# 안전 범위 상수
SAFE_RTH_RANGE = (0.12, 0.30)
SAFE_EXT_RANGE = (0.18, 0.38)

def _clamp(v: float, lo: float, hi: float) -> float:
    """값을 안전 범위로 클램프"""
    return max(lo, min(hi, v))

class Settings:
    """중앙집중식 설정"""
    def __init__(self):
        # 세션 컷오프(스케줄러 레벨)
        self.SIGNAL_CUTOFF_RTH = float(os.getenv("SIGNAL_CUTOFF_RTH", "0.18"))
        self.SIGNAL_CUTOFF_EXT = float(os.getenv("SIGNAL_CUTOFF_EXT", "0.28"))

        # 기획 개선: 임계값 단일 소스 통일 - 모든 임계값의 기준선
        self.MIXER_THRESHOLD = float(os.getenv("MIXER_THRESHOLD", "0.20"))  # 단일 소스 기준선

        self.EDGAR_BONUS = float(os.getenv("EDGAR_BONUS", "0.10"))
        self.COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "180"))  # 원래 기획: 3분
        self.COOL_IMPROVE_MIN = float(os.getenv("COOL_IMPROVE_MIN", "0.10"))  # 원래 기획 톤으로 복원

        self.EXT_MIN_DOLLAR_VOL_5M = float(os.getenv("EXT_MIN_DOLLAR_VOL_5M", "100000"))
        self.EXT_MAX_SPREAD_BP = float(os.getenv("EXT_MAX_SPREAD_BP", "200"))

        # LLM/EDGAR 없을 때 가중치 재정규화 비활성화 (레짐 가중치 유지)
        self.RENORM_NO_SENTIMENT = os.getenv("RENORM_NO_SENTIMENT", "false").lower() in ("true", "1", "yes", "on")

        # 레짐별 가중치
        self.REGIME_WEIGHTS = {
            "trend": {"tech": 0.75, "sentiment": 0.25},
            "vol_spike": {"tech": 0.30, "sentiment": 0.70},
            "mean_revert": {"tech": 0.60, "sentiment": 0.40},
            "sideways": {"tech": 0.50, "sentiment": 0.50},
        }

        # --- Universe Expansion & Tier System ---
        # 기획 개선: 정상 주식 중심으로 재구성, 인버스 ETF는 소수만 유지
        self.TIER_A_TICKERS = os.getenv("TIER_A_TICKERS", "NVDA,AAPL,MSFT,TSLA").split(",")
        self.TIER_B_TICKERS = os.getenv("TIER_B_TICKERS", "AMZN,GOOGL,META,SQQQ").split(",") 
        self.BENCH_TICKERS = os.getenv("BENCH_TICKERS", "AMD,AVGO,NFLX,SOXS").split(",")
        
        self.TIER_A_INTERVAL_SEC = int(os.getenv("TIER_A_INTERVAL_SEC", "30"))
        self.TIER_B_INTERVAL_SEC = int(os.getenv("TIER_B_INTERVAL_SEC", "60"))

        # --- API Rate Limiting ---
        self.API_CALLS_PER_MINUTE = int(os.getenv("API_CALLS_PER_MINUTE", "10"))
        self.API_TIER_A_ALLOCATION = int(os.getenv("API_TIER_A_ALLOCATION", "6"))
        self.API_TIER_B_ALLOCATION = int(os.getenv("API_TIER_B_ALLOCATION", "3"))
        self.API_RESERVE_ALLOCATION = int(os.getenv("API_RESERVE_ALLOCATION", "1"))

        # --- LLM Gating System ---
        self.LLM_DAILY_CALL_LIMIT = int(os.getenv("LLM_DAILY_CALL_LIMIT", "120"))
        self.LLM_CALL_COST_KRW = int(os.getenv("LLM_CALL_COST_KRW", "667"))
        self.LLM_GATING_ENABLED = os.getenv("LLM_GATING_ENABLED", "true").lower() in ("true", "1", "yes", "on")
        
        # 기획 개선: LLM 점수 기준을 단일 소스 기준으로 통일
        self.LLM_MIN_SIGNAL_SCORE = float(os.getenv("LLM_MIN_SIGNAL_SCORE", "0.25"))
        
        # 기획 개선: 인버스 진입 시 LLM 필수화 + 기존 이벤트 확장  
        self.LLM_REQUIRED_EVENTS = set(os.getenv("LLM_REQUIRED_EVENTS", "edgar,vol_spike,fed_speech,rate_decision,market_news,tech_earnings,basket_inverse_entry,macro_risk_on_off").split(","))
        self.LLM_CACHE_DURATION_MIN = int(os.getenv("LLM_CACHE_DURATION_MIN", "30"))

        # --- Position Sizing Enhancement ---
        self.POSITION_CAP_ENABLED = os.getenv("POSITION_CAP_ENABLED", "true").lower() in ("true", "1", "yes", "on")
        self.POSITION_MAX_EQUITY_PCT = float(os.getenv("POSITION_MAX_EQUITY_PCT", "0.8"))
        self.POSITION_MIN_SLOTS = int(os.getenv("POSITION_MIN_SLOTS", "3"))

        # --- EDGAR Override Items ---
        self.EDGAR_OVERRIDE_ITEMS = os.getenv("EDGAR_OVERRIDE_ITEMS", "1.01,2.02,2.03,8.01").split(",")
        self.REGULATORY_BLOCK_WORDS = os.getenv("REGULATORY_BLOCK_WORDS", "regulatory,litigation,FTC,DoJ,antitrust").split(",")
        
        # --- Inverse ETF Support ---
        self.INVERSE_ETFS = os.getenv("INVERSE_ETFS", "SOXS,SQQQ,SPXS,TZA,SDOW,TECS,DRV,SARK,UVXY").split(",")
        self.LEVERAGED_ETFS = os.getenv("LEVERAGED_ETFS", "SOXS,SQQQ,SPXS,TZA,SDOW,TECS,DRV").split(",")
        
        # --- 가격 상한/분할매수 설정 ---
        self.FRACTIONAL_ENABLED = os.getenv("FRACTIONAL_ENABLED", "true").lower() in ("true", "1", "yes", "on")
        self.MAX_PRICE_PER_SHARE = float(os.getenv("MAX_PRICE_PER_SHARE_USD", "120"))
        
        # --- 테스트/개발 모드 설정 ---
        self.TEST_MODE_ENABLED = os.getenv("TEST_MODE_ENABLED", "false").lower() in ("true", "1", "yes", "on")
        self.DISABLE_REAL_TRADING = os.getenv("DISABLE_REAL_TRADING", "false").lower() in ("true", "1", "yes", "on")  
        self.DISABLE_SLACK_ALERTS = os.getenv("DISABLE_SLACK_ALERTS", "false").lower() in ("true", "1", "yes", "on")
        
        # --- 비대칭 임계값 설계 (기획 개선) ---
        # 롱 진입은 상대적으로 완화, 인버스 진입은 엄격
        self.BUY_THRESHOLD = float(os.getenv("BUY_THRESHOLD", "0.20"))  # 롱 진입
        self.SELL_THRESHOLD = float(os.getenv("SELL_THRESHOLD", "-0.20"))  # 롱 청산
        self.INVERSE_ENTRY_MIN_SCORE = float(os.getenv("INVERSE_ENTRY_MIN_SCORE", "0.30"))  # 인버스 진입 최소 절대값
        
        # --- 인버스 전용 가드레일 (기획 조정: 너무 보수적이면 안됨) ---
        self.COOLDOWN_INVERSE_SEC = int(os.getenv("COOLDOWN_INVERSE_SEC", "300"))  # 인버스 쿨다운 5분 (10분은 너무 길어)
        self.DIRECTION_LOCK_INVERSE_SEC = int(os.getenv("DIRECTION_LOCK_INVERSE_SEC", "300"))  # 인버스 방향락 5분  
        self.STOP_LOSS_PCT_INVERSE = float(os.getenv("STOP_LOSS_PCT_INVERSE", "0.03"))  # 인버스 스탑로스 3%
        
        # --- 바스켓 라우팅 잡음 방지 (기획 조정: 0.60은 너무 극단적) ---
        self.BASKET_WINDOW_SEC = int(os.getenv("BASKET_WINDOW_SEC", "300"))  # 5분 윈도우 (좋음)
        self.BASKET_MIN_SIGNALS = int(os.getenv("BASKET_MIN_SIGNALS", "3"))  # 최소 신호 개수 (좋음)
        self.BASKET_NEG_FRACTION = float(os.getenv("BASKET_NEG_FRACTION", "0.45"))  # 네거티브 비율 45% (0.60은 너무 극단)
        self.BASKET_MEAN_THRESHOLD = float(os.getenv("BASKET_MEAN_THRESHOLD", "-0.12"))  # 평균 임계값 (적절)
        
        # --- 예산 일원화 (한화 100만원 기준) ---
        self.SIZING_EQUITY_MODE = os.getenv("SIZING_EQUITY_MODE", "override")  # 예산 기준 강제
        self.SIZING_EQUITY_KRW = int(os.getenv("SIZING_EQUITY_KRW", "1000000"))  # 100만원 예산
        self.USD_KRW_RATE = float(os.getenv("USD_KRW_RATE", "1350"))  # 환율 (동적 업데이트 필요)
        self.RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "0.008"))  # 거래당 0.8% 위험
        self.MAX_CONCURRENT_RISK = float(os.getenv("MAX_CONCURRENT_RISK", "0.04"))  # 최대 동시 위험 4%
        self.MAX_NOTIONAL_PER_TRADE_KRW = int(os.getenv("MAX_NOTIONAL_PER_TRADE_KRW", "250000"))  # 거래당 최대 25만원

settings = Settings()

def get_signal_cutoffs():
    """Redis 값이 있으면 우선 사용, 없으면 기본 설정값 반환. 안전 범위로 클램프"""
    rth, ext = settings.SIGNAL_CUTOFF_RTH, settings.SIGNAL_CUTOFF_EXT
    try:
        rurl = os.getenv("REDIS_URL")
        if rurl:
            r = redis.from_url(rurl)
            rv = r.get("cfg:signal_cutoff:rth")
            ev = r.get("cfg:signal_cutoff:ext")
            if rv is not None:
                rth = float(rv)
            if ev is not None:
                ext = float(ev)
    except Exception:
        pass
    # 임시 델타 적용(테스트 가속): SIGNAL_CUTOFF_RTH_DELTA, SIGNAL_CUTOFF_EXT_DELTA
    try:
        delta_rth = float(os.getenv("SIGNAL_CUTOFF_RTH_DELTA", "0.0"))
        delta_ext = float(os.getenv("SIGNAL_CUTOFF_EXT_DELTA", "0.0"))
        rth = rth + delta_rth
        ext = ext + delta_ext
    except Exception:
        pass
    # 안전 범위로 최종 클램프
    rth = _clamp(rth, *SAFE_RTH_RANGE)
    ext = _clamp(ext, *SAFE_EXT_RANGE)
    return rth, ext

def sanitize_cutoffs_in_redis():
    """Redis의 컷오프 값들을 안전 범위로 정화하고 되써주기"""
    try:
        rurl = os.getenv("REDIS_URL")
        if not rurl:
            return
        r = redis.from_url(rurl)
        rth, ext = get_signal_cutoffs()  # 이미 클램프된 값
        r.set("cfg:signal_cutoff:rth", rth)
        r.set("cfg:signal_cutoff:ext", ext)
        return {"rth": rth, "ext": ext}
    except Exception:
        pass
    return None
