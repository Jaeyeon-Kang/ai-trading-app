"""
실시간 뉴스 스캐너
Alpha Vantage News API를 활용한 실시간 시장 뉴스 수집

기획 목표:
- 파월 발언, Fed 관련 뉴스 실시간 포착
- 기술주 실적, 시장 이벤트 즉시 반영  
- EDGAR 공시의 한계를 뉴스로 보완
"""

import os
import time
import logging
from typing import Dict, List, Optional
from datetime import datetime, timedelta
import httpx

logger = logging.getLogger(__name__)

class NewsScanner:
    """Alpha Vantage News API 기반 실시간 뉴스 스캐너"""
    
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("ALPHA_VANTAGE_API_KEY", "demo")
        self.base_url = "https://www.alphavantage.co/query"
        
        # Fed 관련 키워드 (파월 발언 등)
        self.fed_keywords = [
            "powell", "jerome powell", "federal reserve", "fed chair", 
            "interest rate", "rate cut", "rate hike", "monetary policy",
            "fomc", "fed meeting", "inflation", "fed decision"
        ]
        
        # 기술주 키워드  
        self.tech_keywords = [
            "nvidia", "apple", "microsoft", "tesla", "amazon", 
            "google", "meta", "earnings", "ai", "semiconductor"
        ]
    
    def _get_headers(self) -> Dict[str, str]:
        """API 헤더"""
        return {
            "User-Agent": "AI-Trading-Bot/1.0",
            "Accept": "application/json"
        }
    
    def scan_market_news(self, limit: int = 50) -> List[Dict]:
        """시장 뉴스 스캔"""
        try:
            # Alpha Vantage News API 호출
            params = {
                "function": "NEWS_SENTIMENT",
                "tickers": "AAPL,MSFT,NVDA,TSLA,AMZN,GOOGL,META",  # 주요 종목
                "topics": "technology,financial_markets,economy_macro",
                "time_from": (datetime.now() - timedelta(hours=6)).strftime("%Y%m%dT%H%M"),
                "limit": limit,
                "apikey": self.api_key
            }
            
            with httpx.Client(headers=self._get_headers(), timeout=15.0) as client:
                response = client.get(self.base_url, params=params)
                response.raise_for_status()
                data = response.json()
            
            return self._process_news_data(data)
            
        except Exception as e:
            logger.error(f"뉴스 스캔 실패: {e}")
            return []
    
    def scan_fed_news(self, limit: int = 20) -> List[Dict]:
        """Fed 관련 뉴스 전용 스캔"""
        try:
            params = {
                "function": "NEWS_SENTIMENT", 
                "topics": "economy_macro,financial_markets",
                "time_from": (datetime.now() - timedelta(hours=12)).strftime("%Y%m%dT%H%M"),
                "limit": limit,
                "apikey": self.api_key
            }
            
            with httpx.Client(headers=self._get_headers(), timeout=15.0) as client:
                response = client.get(self.base_url, params=params)
                response.raise_for_status()
                data = response.json()
            
            # Fed 키워드 필터링
            news_items = self._process_news_data(data)
            fed_news = []
            
            for item in news_items:
                title = item.get("title", "").lower()
                summary = item.get("summary", "").lower()
                content = title + " " + summary
                
                if any(keyword in content for keyword in self.fed_keywords):
                    item["event_type"] = "fed_speech" if "powell" in content else "rate_decision" 
                    item["priority"] = "high"
                    fed_news.append(item)
            
            return fed_news
            
        except Exception as e:
            logger.error(f"Fed 뉴스 스캔 실패: {e}")
            return []
    
    def _process_news_data(self, data: Dict) -> List[Dict]:
        """뉴스 데이터 처리"""
        if not data or "feed" not in data:
            return []
        
        processed_news = []
        
        for item in data["feed"]:
            try:
                # 기본 정보 추출
                news_item = {
                    "title": item.get("title", ""),
                    "summary": item.get("summary", "")[:500],  # 500자 제한
                    "url": item.get("url", ""),
                    "published_at": item.get("time_published", ""),
                    "source": item.get("source", ""),
                    "sentiment_score": float(item.get("overall_sentiment_score", 0.0)),
                    "sentiment_label": item.get("overall_sentiment_label", "Neutral"),
                    "relevance_score": self._calculate_relevance(item),
                    "tickers": [ticker.get("ticker", "") for ticker in item.get("ticker_sentiment", [])],
                    "event_type": "market_news",  # 기본값
                    "priority": "medium"
                }
                
                # 고품질 뉴스만 선택
                if news_item["relevance_score"] > 0.3:
                    processed_news.append(news_item)
                    
            except Exception as e:
                logger.warning(f"뉴스 아이템 처리 실패: {e}")
                continue
        
        # 관련성 점수 기준 정렬
        processed_news.sort(key=lambda x: x["relevance_score"], reverse=True)
        return processed_news
    
    def _calculate_relevance(self, item: Dict) -> float:
        """뉴스 관련성 점수 계산"""
        try:
            title = item.get("title", "").lower()
            summary = item.get("summary", "").lower()
            
            relevance = 0.0
            
            # Fed 관련성 (최고 점수)
            if any(keyword in title + summary for keyword in self.fed_keywords):
                relevance += 0.8
            
            # 기술주 관련성
            if any(keyword in title + summary for keyword in self.tech_keywords):
                relevance += 0.6
                
            # 티커 관련성  
            ticker_sentiment = item.get("ticker_sentiment", [])
            if ticker_sentiment:
                max_relevance = max(float(t.get("relevance_score", 0)) for t in ticker_sentiment)
                relevance += max_relevance * 0.5
            
            # 전체 감정 점수 반영
            sentiment_score = abs(float(item.get("overall_sentiment_score", 0)))
            relevance += sentiment_score * 0.3
            
            return min(relevance, 1.0)  # 1.0 상한
            
        except Exception:
            return 0.0
    
    def run_full_scan(self) -> Dict[str, List[Dict]]:
        """전체 뉴스 스캔 실행"""
        logger.info("📰 실시간 뉴스 스캔 시작")
        
        results = {
            "fed_news": self.scan_fed_news(),
            "market_news": self.scan_market_news(),
            "timestamp": datetime.now().isoformat()
        }
        
        total_news = len(results["fed_news"]) + len(results["market_news"])
        high_priority = len([n for n in results["fed_news"] if n.get("priority") == "high"])
        
        logger.info(f"📰 뉴스 스캔 완료: 총 {total_news}건 (고우선순위 {high_priority}건)")
        
        return results

# 싱글톤 인스턴스
_news_scanner = None

def get_news_scanner() -> NewsScanner:
    """뉴스 스캐너 싱글톤"""
    global _news_scanner
    if _news_scanner is None:
        _news_scanner = NewsScanner()
    return _news_scanner