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

        # 믹서 최소 임계(노이즈 컷). 스케줄러 컷과 역할 다름
        self.MIXER_THRESHOLD = float(os.getenv("MIXER_THRESHOLD", "0.15"))  # 원래 기획 톤으로 복원

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
        self.TIER_A_TICKERS = os.getenv("TIER_A_TICKERS", "NVDA,TSLA,AAPL").split(",")
        self.TIER_B_TICKERS = os.getenv("TIER_B_TICKERS", "MSFT,AMZN,META").split(",")
        self.BENCH_TICKERS = os.getenv("BENCH_TICKERS", "GOOGL,AMD,AVGO").split(",")
        
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
        
        self.LLM_MIN_SIGNAL_SCORE = float(os.getenv("LLM_MIN_SIGNAL_SCORE", "0.7"))
        self.LLM_CACHE_DURATION_MIN = int(os.getenv("LLM_CACHE_DURATION_MIN", "30"))

        # --- Position Sizing Enhancement ---
        self.POSITION_CAP_ENABLED = os.getenv("POSITION_CAP_ENABLED", "true").lower() in ("true", "1", "yes", "on")
        self.POSITION_MAX_EQUITY_PCT = float(os.getenv("POSITION_MAX_EQUITY_PCT", "0.8"))
        self.POSITION_MIN_SLOTS = int(os.getenv("POSITION_MIN_SLOTS", "3"))

        # --- EDGAR Override Items ---
        self.EDGAR_OVERRIDE_ITEMS = os.getenv("EDGAR_OVERRIDE_ITEMS", "1.01,2.02,2.03,8.01").split(",")
        self.REGULATORY_BLOCK_WORDS = os.getenv("REGULATORY_BLOCK_WORDS", "regulatory,litigation,FTC,SEC,DoJ,antitrust").split(",")
        
        # --- Inverse ETF Support ---
        self.INVERSE_ETFS = os.getenv("INVERSE_ETFS", "SOXS,SQQQ,SPXS,TZA,SDOW,TECS,DRV,SARK,UVXY").split(",")
        self.LEVERAGED_ETFS = os.getenv("LEVERAGED_ETFS", "SOXS,SQQQ,SPXS,TZA,SDOW,TECS,DRV").split(",")

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