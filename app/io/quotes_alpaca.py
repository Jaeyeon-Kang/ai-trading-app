"""
Alpaca 실시간 시세 인제스터
Alpaca Data API를 통한 1분봉 데이터 수집
"""
import os
import logging
from datetime import datetime, timedelta
from typing import Dict, List
from dataclasses import dataclass
import importlib

logger = logging.getLogger(__name__)

@dataclass
class Candle:
    """캔들 데이터 (기존 코드 호환: o/h/l/c/v, ts)"""
    o: float
    h: float
    l: float
    c: float
    v: int
    ts: datetime

class AlpacaQuotesIngestor:
    """Alpaca 시세 인제스터"""
    
    def __init__(self):
        """초기화"""
        try:
            mod_hist = importlib.import_module("alpaca.data.historical")
            mod_req = importlib.import_module("alpaca.data.requests")
            mod_tf = importlib.import_module("alpaca.data.timeframe")
            client_cls = getattr(mod_hist, "StockHistoricalDataClient")
            self._StockBarsRequest = getattr(mod_req, "StockBarsRequest")
            self._TimeFrame = getattr(mod_tf, "TimeFrame")
        except Exception as e:  # noqa: BLE001
            logger.error("Alpaca SDK import 실패: %s", e)
            raise

        self.client = client_cls(
            api_key=os.getenv("ALPACA_API_KEY"),
            secret_key=os.getenv("ALPACA_API_SECRET")
        )
        self.cache = {}  # 캔들 데이터 캐시
        self.tickers = []  # 티커 리스트 (호환성)
        logger.info("Alpaca Quotes 인제스터 초기화 완료")
    
    def get_latest_candles(self, ticker: str, n: int = 100) -> List[Candle]:
        """최근 n개 캔들 조회"""
        try:
            # 1분봉 요청
            request = self._StockBarsRequest(
                symbol_or_symbols=[ticker],
                timeframe=self._TimeFrame.Minute,
                start=datetime.now() - timedelta(days=5),  # 5일 전부터
                limit=n
            )
            
            bars = self.client.get_stock_bars(request)
            candles: List[Candle] = []

            # alpaca-py 호환 처리: bars가 dict/객체/DF 등 다양한 형태
            series: List = []
            try:
                # dict 스타일
                if isinstance(bars, dict):
                    series = bars.get(ticker, []) or []
                # data 속성(dict)
                elif hasattr(bars, "data"):
                    data = getattr(bars, "data")
                    if isinstance(data, dict):
                        series = data.get(ticker, []) or []
                    else:
                        series = list(data) if data is not None else []
                # DataFrame 제공 시
                elif hasattr(bars, "df"):
                    df = getattr(bars, "df")
                    if df is not None and len(df) > 0:
                        df_t = df[df.get("symbol", "").str.upper() == ticker.upper()]
                        for _, row in df_t.tail(n).iterrows():
                            try:
                                ts_val = row.get("timestamp")
                                ts_parsed = ts_val if isinstance(ts_val, datetime) else datetime.fromisoformat(str(ts_val))
                                candles.append(Candle(
                                    o=float(row["open"]),
                                    h=float(row["high"]),
                                    l=float(row["low"]),
                                    c=float(row["close"]),
                                    v=int(row.get("volume", 0) or 0),
                                    ts=ts_parsed
                                ))
                            except Exception:  # noqa: BLE001
                                continue
                else:
                    # 마지막 폴백: iterable로 간주
                    series = list(bars) if bars is not None else []
            except Exception:  # noqa: BLE001
                series = []

            # Bar 객체 리스트 파싱
            if series:
                for bar in series[-n:]:
                    try:
                        ts = getattr(bar, "timestamp", None)
                        if ts and hasattr(ts, "replace"):
                            ts = ts.replace(tzinfo=None)
                        candles.append(Candle(
                            o=float(getattr(bar, "open", 0.0) or 0.0),
                            h=float(getattr(bar, "high", 0.0) or 0.0),
                            l=float(getattr(bar, "low", 0.0) or 0.0),
                            c=float(getattr(bar, "close", 0.0) or 0.0),
                            v=int(getattr(bar, "volume", 0) or 0),
                            ts=ts or datetime.utcnow()
                        ))
                    except Exception:  # noqa: BLE001
                        continue
            
            # 캐시 업데이트
            self.cache[ticker] = candles
            logger.debug("Alpaca %s: %d개 캔들 조회", ticker, len(candles))
            return candles
            
        except Exception as e:  # noqa: BLE001
            logger.error("Alpaca %s 캔들 조회 실패: %s", ticker, e)
            return self.cache.get(ticker, [])
    
    def update_all_tickers(self, tickers: List[str] = None):
        """모든 티커 업데이트"""
        if tickers is None:
            tickers = self.tickers
        
        self.tickers = tickers  # 티커 리스트 저장
        logger.info("Alpaca 전체 티커 업데이트: %d개", len(tickers))
        
        for ticker in tickers:
            try:
                self.get_latest_candles(ticker, 200)
            except Exception as e:  # noqa: BLE001
                logger.error("Alpaca %s 업데이트 실패: %s", ticker, e)
    
    def _compute_indicators_from_candles(self, candles: List[Candle]) -> Dict:
        """지표 계산 (delayed 인제스터와 호환)"""
        if not candles:
            return {"dollar_vol_5m": 0.0, "spread_bp": 0.0}
        # 1분봉 가정: 최근 5개 사용
        window = candles[-5:] if len(candles) >= 5 else candles
        last_close = window[-1].c if window else 0.0
        dollar_vol = sum((c.c or last_close) * float(c.v or 0) for c in window)
        last = window[-1]
        if last.c > 0:
            spread_bp = ((last.h - last.l) / max(last.c, 1e-9)) * 10000.0
        else:
            spread_bp = 0.0
        return {"dollar_vol_5m": float(dollar_vol), "spread_bp": float(spread_bp)}

    def get_technical_indicators(self, ticker: str) -> Dict:
        """기술지표 반환 (delayed 인제스터와 동일 키)"""
        candles = self.cache.get(ticker)
        if not candles:
            try:
                candles = self.get_latest_candles(ticker, 50)
            except Exception:  # noqa: BLE001
                candles = []
        current_price = candles[-1].c if candles else 0.0
        indicators = self._compute_indicators_from_candles(candles)
        return {
            "current_price": current_price,
            "dollar_vol_5m": indicators.get("dollar_vol_5m", 0.0),
            "spread_bp": indicators.get("spread_bp", 0.0)
        }
    
    def update(self):
        """기존 티커 업데이트 (호환성 메소드)"""
        self.update_all_tickers()
    
    def update_universe_tickers(self, tickers: List[str]):
        """유니버스 티커 업데이트"""
        self.update_all_tickers(tickers)
    
    def warmup_backfill(self, tickers: List[str], days_back: int = 5):
        """워밍업 백필"""
        logger.info("Alpaca 워밍업 백필: %d개 티커, %d일", len(tickers), days_back)
        for ticker in tickers:
            try:
                self.get_latest_candles(ticker, days_back * 390)  # 6.5시간 * 60분
            except Exception as e:  # noqa: BLE001
                logger.error("Alpaca %s 워밍업 실패: %s", ticker, e)
    
    def get_cached_candles(self, ticker: str) -> List[Candle]:
        """캐시된 캔들 조회"""
        return self.cache.get(ticker, [])
    
    def get_market_data_summary(self) -> Dict:
        """티커별 시세/지표 맵 (scheduler.update_quotes 호환)"""
        result: Dict[str, Dict] = {}
        for ticker, candles in self.cache.items():
            try:
                candles = candles or []
                last_close = float(candles[-1].c) if candles else 0.0
                indicators = self._compute_indicators_from_candles(candles)
                last_ts = candles[-1].ts if candles else datetime.utcnow()
                result[ticker] = {
                    "current_price": last_close,
                    "indicators": indicators,
                    "last_update": last_ts,
                }
            except Exception:  # noqa: BLE001
                continue
        return result