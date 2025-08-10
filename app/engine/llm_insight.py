"""
LLM 인사이트 엔진 (해석 전용, 검색 금지)
헤드라인/EDGAR 스니펫(≤1000자) → JSON(점수/원인/기간/요약), 캐시·비용가드
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
import os

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
    sentiment: float  # -1 ~ +1
    trigger: str  # 원인
    horizon_minutes: int  # 영향 지속 시간 (분)
    summary: str  # 한 줄 요약
    timestamp: datetime
    cost_krw: float  # API 비용 (원화)

class LLMInsightEngine:
    """LLM 인사이트 엔진 (해석 전용)"""
    
    def __init__(self, api_key: Optional[str] = None, monthly_cap_krw: float = 80000):
        """
        Args:
            api_key: OpenAI API 키 (None이면 환경변수에서 로드)
            monthly_cap_krw: 월 비용 상한 (원화)
        """
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            raise ValueError("OpenAI API 키가 필요합니다")
        
        self.monthly_cap_krw = monthly_cap_krw
        
        # 비용 추적 (원화 기준)
        self.monthly_cost_krw = 0.0
        self.monthly_start = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        
        # 캐시 (24시간 TTL)
        self.cache: Dict[str, Dict] = {}
        self.cache_ttl_hours = 24
        
        # 속도 제한 (분당 1콜)
        self.rate_limit_calls_per_minute = 1
        self.last_call_time = None
        
        # 상태
        self.status = LLMStatus.ACTIVE
        
        # OpenAI 클라이언트
        openai.api_key = self.api_key
        
        # 환율 (고정값, 실제로는 API로 가져와야 함)
        self.usd_to_krw = 1300
        
        logger.info(f"LLM 인사이트 엔진 초기화: 월 상한 {monthly_cap_krw:,}원")
    
    def analyze_text(self, text: str, source: str = "unknown") -> Optional[LLMInsight]:
        """
        텍스트 분석 (해석 전용)
        
        Args:
            text: 분석할 텍스트 (≤1000자)
            source: 텍스트 출처 (URL 등)
            
        Returns:
            LLMInsight: 분석 결과 (None if 제한/에러)
        """
        # 텍스트 길이 제한
        if len(text) > 1000:
            text = text[:1000]
            logger.warning(f"텍스트가 1000자를 초과하여 잘렸습니다")
        
        # 캐시 확인 (URL 기반)
        cache_key = self._generate_cache_key(text, source)
        cached_result = self._get_cached_result(cache_key)
        if cached_result:
            logger.debug(f"캐시 히트: {source}")
            return cached_result
        
        # 제한 확인
        if not self._check_limits():
            logger.warning(f"LLM 제한 도달: {self.status.value}")
            return None
        
        try:
            # LLM 호출
            result = self._call_llm(text, source)
            
            # 캐시 저장
            self._cache_result(cache_key, result)
            
            # 비용 추적
            self._update_cost(result.cost_krw)
            
            logger.info(f"LLM 분석 완료: {source} (점수: {result.sentiment:.2f})")
            return result
            
        except Exception as e:
            logger.error(f"LLM 분석 실패 ({source}): {e}")
            self.status = LLMStatus.ERROR
            return None
    
    def _call_llm(self, text: str, source: str) -> LLMInsight:
        """LLM API 호출 (해석 전용)"""
        # 프롬프트 구성
        prompt = self._build_prompt(text, source)
        
        # API 호출
        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",  # 비용 절약을 위해 mini 사용
            messages=[
                {
                    "role": "system", 
                    "content": "당신은 금융 분석가입니다. 주어진 텍스트를 분석하여 JSON 형태로 응답하세요. 검색하지 말고 텍스트만 해석하세요."
                },
                {"role": "user", "content": prompt}
            ],
            max_tokens=150,
            temperature=0.1
        )
        
        # 응답 파싱
        content = response.choices[0].message.content
        result_data = self._parse_llm_response(content)
        
        # 비용 계산 (원화)
        input_tokens = response.usage.prompt_tokens
        output_tokens = response.usage.completion_tokens
        cost_usd = (input_tokens * 0.00015 + output_tokens * 0.0006) / 1000
        cost_krw = cost_usd * self.usd_to_krw
        
        return LLMInsight(
            sentiment=result_data.get("sentiment", 0.0),
            trigger=result_data.get("trigger", "unknown"),
            horizon_minutes=result_data.get("horizon_minutes", 60),
            summary=result_data.get("summary", ""),
            timestamp=datetime.now(),
            cost_krw=cost_krw
        )
    
    def _build_prompt(self, text: str, source: str) -> str:
        """프롬프트 구성"""
        return f"""
다음 금융 텍스트를 분석하세요:

텍스트: {text}
출처: {source}

다음 JSON 형식으로만 응답하세요:
{{
    "sentiment": -1.0에서 1.0 사이의 실수 (매우 부정적 ~ 매우 긍정적),
    "trigger": 감정을 일으킨 원인 (문자열),
    "horizon_minutes": 이 감정이 지속될 시간 (분, 15-480),
    "summary": 한 줄 요약 (한국어)
}}

예시:
{{
    "sentiment": 0.7,
    "trigger": "실적 예상치 상회",
    "horizon_minutes": 120,
    "summary": "실적 예상치 상회로 긍정적"
}}

JSON 응답:"""
    
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
            sentiment = float(result.get("sentiment", 0.0))
            sentiment = max(-1.0, min(1.0, sentiment))  # -1~1 범위로 제한
            
            horizon = int(result.get("horizon_minutes", 60))
            horizon = max(15, min(480, horizon))  # 15~480분 범위로 제한
            
            return {
                "sentiment": sentiment,
                "trigger": str(result.get("trigger", "unknown")),
                "horizon_minutes": horizon,
                "summary": str(result.get("summary", ""))
            }
            
        except Exception as e:
            logger.error(f"LLM 응답 파싱 실패: {e}")
            return {
                "sentiment": 0.0,
                "trigger": "parsing_error",
                "horizon_minutes": 60,
                "summary": "분석 실패"
            }
    
    def _generate_cache_key(self, text: str, source: str) -> str:
        """캐시 키 생성 (URL 기반)"""
        # 소스(URL) 기반 해시
        source_hash = hashlib.md5(source.encode()).hexdigest()
        # 텍스트 해시 (첫 200자만)
        text_hash = hashlib.md5(text[:200].encode()).hexdigest()
        return f"{source_hash}_{text_hash}"
    
    def _get_cached_result(self, cache_key: str) -> Optional[LLMInsight]:
        """캐시된 결과 조회"""
        if cache_key not in self.cache:
            return None
        
        cached = self.cache[cache_key]
        cached_time = datetime.fromisoformat(cached["timestamp"])
        
        # TTL 확인 (24시간)
        if datetime.now() - cached_time > timedelta(hours=self.cache_ttl_hours):
            del self.cache[cache_key]
            return None
        
        # LLMInsight 객체로 변환
        return LLMInsight(
            sentiment=cached["sentiment"],
            trigger=cached["trigger"],
            horizon_minutes=cached["horizon_minutes"],
            summary=cached["summary"],
            timestamp=cached_time,
            cost_krw=0.0  # 캐시된 결과는 비용 0
        )
    
    def _cache_result(self, cache_key: str, result: LLMInsight):
        """결과 캐시 저장"""
        self.cache[cache_key] = {
            "sentiment": result.sentiment,
            "trigger": result.trigger,
            "horizon_minutes": result.horizon_minutes,
            "summary": result.summary,
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
        
        # 월 비용 제한 (원화 기준)
        if now.month != self.monthly_start.month:
            self.monthly_cost_krw = 0.0
            self.monthly_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        
        if self.monthly_cost_krw >= self.monthly_cap_krw:
            self.status = LLMStatus.COST_LIMITED
            logger.warning(f"월 비용 한도 도달: {self.monthly_cost_krw:,.0f}원 / {self.monthly_cap_krw:,.0f}원")
            return False
        
        # 분당 호출 제한 (1콜)
        if self.last_call_time:
            time_diff = (now - self.last_call_time).total_seconds()
            if time_diff < 60:  # 1분 미만
                self.status = LLMStatus.RATE_LIMITED
                logger.warning(f"분당 호출 제한: {60 - time_diff:.1f}초 대기 필요")
                return False
        
        self.status = LLMStatus.ACTIVE
        return True
    
    def _update_cost(self, cost_krw: float):
        """비용 업데이트"""
        self.monthly_cost_krw += cost_krw
        self.last_call_time = datetime.now()
        
        logger.debug(f"비용 업데이트: +{cost_krw:.0f}원 (총 {self.monthly_cost_krw:,.0f}원)")
    
    def get_status(self) -> Dict[str, Any]:
        """상태 정보"""
        return {
            "status": self.status.value,
            "monthly_cost_krw": self.monthly_cost_krw,
            "monthly_cap_krw": self.monthly_cap_krw,
            "cache_size": len(self.cache),
            "last_call": self.last_call_time.isoformat() if self.last_call_time else None,
            "is_active": self.status == LLMStatus.ACTIVE
        }
    
    def reset_limits(self):
        """제한 리셋 (테스트용)"""
        self.monthly_cost_krw = 0.0
        self.last_call_time = None
        self.status = LLMStatus.ACTIVE
        logger.info("LLM 제한 리셋 완료")
    
    def analyze_edgar_filing(self, filing_data: Dict) -> Optional[LLMInsight]:
        """EDGAR 공시 분석"""
        ticker = filing_data.get("ticker", "")
        form_type = filing_data.get("form_type", "")
        summary = filing_data.get("summary", "")
        items = filing_data.get("items", [])
        
        # 공시 타입별 텍스트 구성 (1000자 제한)
        if form_type == "8-K":
            text = f"8-K filing for {ticker}: Items {', '.join(items[:3])}. {summary}"
        elif form_type == "4":
            text = f"Form 4 filing for {ticker}: Insider trading activity. {summary}"
        else:
            text = f"{form_type} filing for {ticker}: {summary}"
        
        # 1000자 제한
        if len(text) > 1000:
            text = text[:1000]
        
        return self.analyze_text(text, f"EDGAR_{form_type}_{ticker}")
    
    def analyze_news_headline(self, headline: str, url: str) -> Optional[LLMInsight]:
        """뉴스 헤드라인 분석"""
        return self.analyze_text(headline, url)
