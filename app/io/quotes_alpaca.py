"""
Alpaca 실시간 시세 인제스터
Alpaca Data API를 통한 1분봉 데이터 수집
"""
import os
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

logger = logging.getLogger(__name__)

@dataclass
class Candle:
    """캔들 데이터"""
    open: float
    high: float
    low: float
    close: float
    volume: int
    timestamp: datetime

class AlpacaQuotesIngestor:
    """Alpaca 시세 인제스터"""
    
    def __init__(self):
        """초기화"""
        self.client = StockHistoricalDataClient(
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
            request = StockBarsRequest(
                symbol_or_symbols=[ticker],
                timeframe=TimeFrame.Minute,
                start=datetime.now() - timedelta(days=5),  # 5일 전부터
                limit=n
            )
            
            bars = self.client.get_stock_bars(request)
            candles = []
            
            if ticker in bars:
                for bar in bars[ticker]:
                    candle = Candle(
                        open=float(bar.open),
                        high=float(bar.high),
                        low=float(bar.low),
                        close=float(bar.close),
                        volume=int(bar.volume),
                        timestamp=bar.timestamp.replace(tzinfo=None)
                    )
                    candles.append(candle)
            
            # 캐시 업데이트
            self.cache[ticker] = candles
            logger.debug(f"Alpaca {ticker}: {len(candles)}개 캔들 조회")
            return candles
            
        except Exception as e:
            logger.error(f"Alpaca {ticker} 캔들 조회 실패: {e}")
            return self.cache.get(ticker, [])
    
    def update_all_tickers(self, tickers: List[str] = None):
        """모든 티커 업데이트"""
        if tickers is None:
            tickers = self.tickers
        
        self.tickers = tickers  # 티커 리스트 저장
        logger.info(f"Alpaca 전체 티커 업데이트: {len(tickers)}개")
        
        for ticker in tickers:
            try:
                self.get_latest_candles(ticker, 200)
            except Exception as e:
                logger.error(f"Alpaca {ticker} 업데이트 실패: {e}")
    
    def update(self):
        """기존 티커 업데이트 (호환성 메소드)"""
        self.update_all_tickers()
    
    def update_universe_tickers(self, tickers: List[str]):
        """유니버스 티커 업데이트"""
        self.update_all_tickers(tickers)
    
    def warmup_backfill(self, tickers: List[str], days_back: int = 5):
        """워밍업 백필"""
        logger.info(f"Alpaca 워밍업 백필: {len(tickers)}개 티커, {days_back}일")
        for ticker in tickers:
            try:
                self.get_latest_candles(ticker, days_back * 390)  # 6.5시간 * 60분
            except Exception as e:
                logger.error(f"Alpaca {ticker} 워밍업 실패: {e}")
    
    def get_cached_candles(self, ticker: str) -> List[Candle]:
        """캐시된 캔들 조회"""
        return self.cache.get(ticker, [])
    
    def get_market_data_summary(self) -> Dict:
        """시장 데이터 요약 (호환성 메소드)"""
        return {
            "total_tickers": len(self.tickers),
            "cached_tickers": len(self.cache),
            "provider": "alpaca",
            "status": "active"
        }