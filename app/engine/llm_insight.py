"""
LLM 인사이트 엔진
헤드라인/공시 텍스트 → JSON(점수/원인/기간/요약), 캐시·비용가드
"""
import openai
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
import hashlib
import time
from enum import Enum

logger = logging.getLogger(__name__)

class LLMStatus(Enum):
    """LLM 상태"""
    ACTIVE = "active"
    RATE_LIMITED = "rate_limited"
    COST_LIMITED = "cost_limited"
    ERROR = "error"

@dataclass
class LLMInsight:
    """LLM 인사이트 결과"""
    ticker: str
    sentiment_score: float  # -1 ~ +1
    trigger: str  # 원인
    horizon_minutes: int  # 영향 지속 시간
    summary: str  # 한 줄 요약
    confidence: float  # 0~1
    timestamp: datetime
    cost_usd: float  # API 비용

class LLMInsightEngine:
    """LLM 인사이트 엔진"""
    
    def __init__(self, api_key: str, monthly_cap_krw: float = 80000):
        """
        Args:
            api_key: OpenAI API 키
            monthly_cap_krw: 월 비용 상한 (원화)
        """
        self.api_key = api_key
        self.monthly_cap_krw = monthly_cap_krw
        self.monthly_cap_usd = monthly_cap_krw / 1300  # 약 $60
        
        # 비용 추적
        self.monthly_cost_usd = 0.0
        self.monthly_start = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        
        # 캐시
        self.cache: Dict[str, Dict] = {}
        self.cache_ttl_hours = 24  # 24시간 캐시
        
        # 속도 제한
        self.rate_limit_calls_per_minute = 1
        self.rate_limit_calls_per_day = 200
        self.last_call_time = None
        self.daily_calls = 0
        self.daily_reset = datetime.now().date()
        
        # 상태
        self.status = LLMStatus.ACTIVE
        
        # OpenAI 클라이언트
        openai.api_key = api_key
        
        logger.info(f"LLM 인사이트 엔진 초기화: 월 상한 {monthly_cap_krw:,}원")
    
    def analyze_text(self, ticker: str, text: str, source: str = "unknown") -> Optional[LLMInsight]:
        """
        텍스트 분석
        
        Args:
            ticker: 종목 코드
            text: 분석할 텍스트
            source: 텍스트 출처
            
        Returns:
            LLMInsight: 분석 결과 (None if 제한/에러)
        """
        # 캐시 확인
        cache_key = self._generate_cache_key(ticker, text)
        cached_result = self._get_cached_result(cache_key)
        if cached_result:
            logger.debug(f"캐시 히트: {ticker}")
            return cached_result
        
        # 제한 확인
        if not self._check_limits():
            logger.warning(f"LLM 제한 도달: {self.status.value}")
            return None
        
        try:
            # LLM 호출
            result = self._call_llm(ticker, text, source)
            
            # 캐시 저장
            self._cache_result(cache_key, result)
            
            # 비용 추적
            self._update_cost(result.cost_usd)
            
            logger.info(f"LLM 분석 완료: {ticker} (점수: {result.sentiment_score:.2f})")
            return result
            
        except Exception as e:
            logger.error(f"LLM 분석 실패 ({ticker}): {e}")
            self.status = LLMStatus.ERROR
            return None
    
    def _call_llm(self, ticker: str, text: str, source: str) -> LLMInsight:
        """LLM API 호출"""
        # 프롬프트 구성
        prompt = self._build_prompt(ticker, text, source)
        
        # API 호출
        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",  # 비용 절약을 위해 mini 사용
            messages=[
                {"role": "system", "content": "You are a financial analyst. Analyze the given text and return a JSON response with sentiment score, trigger, horizon, and summary."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=200,
            temperature=0.1
        )
        
        # 응답 파싱
        content = response.choices[0].message.content
        result_data = self._parse_llm_response(content)
        
        # 비용 계산 (GPT-4o-mini: $0.00015/1K input, $0.0006/1K output)
        input_tokens = response.usage.prompt_tokens
        output_tokens = response.usage.completion_tokens
        cost_usd = (input_tokens * 0.00015 + output_tokens * 0.0006) / 1000
        
        return LLMInsight(
            ticker=ticker,
            sentiment_score=result_data.get("sentiment", 0.0),
            trigger=result_data.get("trigger", "unknown"),
            horizon_minutes=result_data.get("horizon_minutes", 60),
            summary=result_data.get("summary", ""),
            confidence=result_data.get("confidence", 0.5),
            timestamp=datetime.now(),
            cost_usd=cost_usd
        )
    
    def _build_prompt(self, ticker: str, text: str, source: str) -> str:
        """프롬프트 구성"""
        return f"""
Analyze this financial text for {ticker} stock:

Text: {text[:500]}...
Source: {source}

Return ONLY a JSON object with these fields:
- sentiment: float between -1 (very negative) and +1 (very positive)
- trigger: string describing what caused the sentiment
- horizon_minutes: integer for how long this sentiment might last (15-480 minutes)
- summary: one-line summary in Korean
- confidence: float between 0 and 1 for how confident you are

Example:
{{
    "sentiment": 0.7,
    "trigger": "earnings beat",
    "horizon_minutes": 120,
    "summary": "실적 예상치 상회로 긍정적",
    "confidence": 0.8
}}

JSON response:"""
    
    def _parse_llm_response(self, content: str) -> Dict[str, Any]:
        """LLM 응답 파싱"""
        try:
            # JSON 추출
            start_idx = content.find('{')
            end_idx = content.rfind('}') + 1
            
            if start_idx == -1 or end_idx == 0:
                raise ValueError("JSON not found in response")
            
            json_str = content[start_idx:end_idx]
            result = json.loads(json_str)
            
            # 값 검증 및 정규화
            return {
                "sentiment": float(result.get("sentiment", 0.0)),
                "trigger": str(result.get("trigger", "unknown")),
                "horizon_minutes": int(result.get("horizon_minutes", 60)),
                "summary": str(result.get("summary", "")),
                "confidence": float(result.get("confidence", 0.5))
            }
            
        except Exception as e:
            logger.error(f"LLM 응답 파싱 실패: {e}")
            return {
                "sentiment": 0.0,
                "trigger": "parsing_error",
                "horizon_minutes": 60,
                "summary": "분석 실패",
                "confidence": 0.0
            }
    
    def _generate_cache_key(self, ticker: str, text: str) -> str:
        """캐시 키 생성"""
        # 텍스트 해시 (첫 100자만)
        text_hash = hashlib.md5(text[:100].encode()).hexdigest()
        return f"{ticker}_{text_hash}"
    
    def _get_cached_result(self, cache_key: str) -> Optional[LLMInsight]:
        """캐시된 결과 조회"""
        if cache_key not in self.cache:
            return None
        
        cached = self.cache[cache_key]
        cached_time = datetime.fromisoformat(cached["timestamp"])
        
        # TTL 확인
        if datetime.now() - cached_time > timedelta(hours=self.cache_ttl_hours):
            del self.cache[cache_key]
            return None
        
        # LLMInsight 객체로 변환
        return LLMInsight(
            ticker=cached["ticker"],
            sentiment_score=cached["sentiment_score"],
            trigger=cached["trigger"],
            horizon_minutes=cached["horizon_minutes"],
            summary=cached["summary"],
            confidence=cached["confidence"],
            timestamp=cached_time,
            cost_usd=0.0  # 캐시된 결과는 비용 0
        )
    
    def _cache_result(self, cache_key: str, result: LLMInsight):
        """결과 캐시 저장"""
        self.cache[cache_key] = {
            "ticker": result.ticker,
            "sentiment_score": result.sentiment_score,
            "trigger": result.trigger,
            "horizon_minutes": result.horizon_minutes,
            "summary": result.summary,
            "confidence": result.confidence,
            "timestamp": result.timestamp.isoformat()
        }
        
        # 캐시 크기 제한 (최대 1000개)
        if len(self.cache) > 1000:
            # 가장 오래된 항목 제거
            oldest_key = min(self.cache.keys(), 
                           key=lambda k: self.cache[k]["timestamp"])
            del self.cache[oldest_key]
    
    def _check_limits(self) -> bool:
        """제한 확인"""
        now = datetime.now()
        
        # 월 비용 제한
        if now.month != self.monthly_start.month:
            self.monthly_cost_usd = 0.0
            self.monthly_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        
        if self.monthly_cost_usd >= self.monthly_cap_usd:
            self.status = LLMStatus.COST_LIMITED
            return False
        
        # 일일 호출 제한
        if now.date() != self.daily_reset:
            self.daily_calls = 0
            self.daily_reset = now.date()
        
        if self.daily_calls >= self.rate_limit_calls_per_day:
            self.status = LLMStatus.RATE_LIMITED
            return False
        
        # 분당 호출 제한
        if self.last_call_time:
            time_diff = (now - self.last_call_time).total_seconds()
            if time_diff < 60:  # 1분 미만
                self.status = LLMStatus.RATE_LIMITED
                return False
        
        self.status = LLMStatus.ACTIVE
        return True
    
    def _update_cost(self, cost_usd: float):
        """비용 업데이트"""
        self.monthly_cost_usd += cost_usd
        self.daily_calls += 1
        self.last_call_time = datetime.now()
    
    def get_status(self) -> Dict[str, Any]:
        """상태 정보"""
        return {
            "status": self.status.value,
            "monthly_cost_usd": self.monthly_cost_usd,
            "monthly_cap_usd": self.monthly_cap_usd,
            "daily_calls": self.daily_calls,
            "daily_limit": self.rate_limit_calls_per_day,
            "cache_size": len(self.cache),
            "last_call": self.last_call_time.isoformat() if self.last_call_time else None
        }
    
    def reset_limits(self):
        """제한 리셋 (테스트용)"""
        self.monthly_cost_usd = 0.0
        self.daily_calls = 0
        self.last_call_time = None
        self.status = LLMStatus.ACTIVE
        logger.info("LLM 제한 리셋 완료")
    
    def analyze_edgar_filing(self, filing_data: Dict) -> Optional[LLMInsight]:
        """EDGAR 공시 분석"""
        ticker = filing_data.get("ticker", "")
        form_type = filing_data.get("form_type", "")
        summary = filing_data.get("summary", "")
        items = filing_data.get("items", [])
        
        # 공시 타입별 텍스트 구성
        if form_type == "8-K":
            text = f"8-K filing for {ticker}: Items {', '.join(items)}. {summary}"
        elif form_type == "4":
            text = f"Form 4 filing for {ticker}: Insider trading activity. {summary}"
        else:
            text = f"{form_type} filing for {ticker}: {summary}"
        
        return self.analyze_text(ticker, text, f"EDGAR_{form_type}")
    
    def analyze_news_headline(self, ticker: str, headline: str, source: str = "news") -> Optional[LLMInsight]:
        """뉴스 헤드라인 분석"""
        return self.analyze_text(ticker, headline, source)
