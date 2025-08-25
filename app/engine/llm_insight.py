"""
LLM 인사이트 엔진
뉴스 헤드라인 및 EDGAR 공시 분석
입력: 헤드라인/EDGAR 스니펫(≤1000자)
출력(JSON): {sentiment:-1~1, trigger:str, horizon_minutes:int, summary:str}
24시간 캐시, 분당 1콜 제한, 월 비용 KRW 기준 집계
→ LLM_MONTHLY_CAP_KRW 넘으면 자동 OFF(그때는 기술신호만)

LLM 호출 조건:
- edgar_event == True OR regime == 'vol_spike'
- RTH(정규장) 시간대만 허용 (09:30-16:00 ET)
"""
import logging
import hashlib
import json
import os
from typing import Dict, Optional
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, time as dtime
import time
import numpy as np
from openai import OpenAI
import redis
from app.config import settings

logger = logging.getLogger(__name__)

@dataclass
class LLMInsight:
    """LLM 분석 결과"""
    sentiment: float  # -1.0 ~ 1.0
    trigger: str      # 주요 트리거
    horizon_minutes: int  # 영향 지속 시간 (분)
    summary: str      # 한 줄 요약
    timestamp: datetime
    cost_krw: float = 0.0  # 비용 (KRW)

class LLMInsightEngine:
    """LLM 인사이트 엔진"""
    
    def __init__(self, 
                 api_key: str = None,
                 monthly_cap_krw: float = 80000.0,  # 월 비용 한도 (KRW)
                 usd_to_krw: float = 1300.0,  # USD/KRW 환율
                 cache_hours: int = None):  # 캐시 시간 (시간, None이면 환경변수 사용)
        """
        Args:
            api_key: OpenAI API 키 (환경변수 OPENAI_API_KEY에서 로드)
            monthly_cap_krw: 월 비용 한도 (KRW)
            usd_to_krw: USD/KRW 환율
            cache_hours: 캐시 시간 (시간, None이면 환경변수에서 로드)
        """
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.monthly_cap_krw = monthly_cap_krw
        self.usd_to_krw = usd_to_krw
        
        # 캐시 시간 설정: 환경변수 우선, 없으면 기본값 24시간
        if cache_hours is not None:
            self.cache_hours = cache_hours
        else:
            cache_minutes = int(os.getenv("LLM_CACHE_DURATION_MIN", "30"))
            self.cache_hours = cache_minutes / 60.0
        
        # OpenAI 클라이언트
        if self.api_key:
            self.client = OpenAI(api_key=self.api_key)
        else:
            self.client = None
            logger.warning("OpenAI API 키가 설정되지 않음")
        
        # 캐시 (메모리 기반, 실제로는 Redis 사용 권장)
        self.cache: Dict[str, Dict] = {}
        
        # 비용 추적
        self.monthly_cost_krw = 0.0
        self.current_month = datetime.now().month
        
        # 속도 제한
        self.rate_limit_calls_per_minute = 1
        self.last_call_time = 0
        
        # LLM 활성화 상태
        self.llm_enabled = True
        
        # 슬랙 봇 참조 (상태 변경 알림용)
        self.slack_bot = None

        # 메트릭 기록용 Redis (선택)
        self.redis = None
        try:
            redis_url = os.getenv("REDIS_URL")
            if redis_url:
                self.redis = redis.from_url(redis_url)
        except Exception:
            self.redis = None
        
        logger.info(f"LLM 인사이트 엔진 초기화: 월 한도 {monthly_cap_krw:,.0f}원, 캐시 {self.cache_hours:.1f}시간")
    
    def set_slack_bot(self, slack_bot):
        """슬랙 봇 설정 (상태 변경 알림용)"""
        self.slack_bot = slack_bot
    
    def should_call_llm(self, edgar_event: bool = False, regime: str = None, signal_strength: float = 0.0) -> bool:
        """
        LLM 호출 조건 확인 (Phase 1.5: 강신호 지원 추가)
        
        Args:
            edgar_event: EDGAR 이벤트 여부
            regime: 현재 레짐 ('trend', 'vol_spike', 'mean_revert', 'sideways')
            signal_strength: 신호 강도 (절댓값, 0.70+ 강신호)
            
        Returns:
            bool: LLM 호출 여부
        """
        # 조건 1: 강신호 무조건 LLM 분석 (Phase 1.5 핵심 개선!)
        if abs(signal_strength) >= settings.LLM_MIN_SIGNAL_SCORE:
            logger.info(f"🎯 강신호 LLM 호출: signal_strength={signal_strength:.3f}")
            return True
        
        # 조건 2: edgar_event == True OR regime == 'vol_spike' (기존 조건 유지)
        if not edgar_event and regime != 'vol_spike':
            logger.debug(f"LLM 호출 조건 불충족: edgar_event={edgar_event}, regime={regime}, signal_strength={signal_strength:.3f}")
            return False
        
        # 조건 2: RTH(정규장) 시간대만 허용 (09:30-16:00 ET) — RTH_ONLY=true일 때만 적용
        rth_only = os.getenv("RTH_ONLY", "false").lower() in ("1", "true", "yes", "on")
        if rth_only and not self._is_rth_time():
            logger.debug("LLM 호출 제한: 정규장 시간이 아님")
            return False
        
        return True
    
    def _is_rth_time(self) -> bool:
        """RTH(정규장) 시간대 확인 (09:30-16:00 ET)"""
        # ET 시간대 (UTC-5, 일광절약시간 고려)
        et_tz = timezone(timedelta(hours=-5))  # EST
        et_time = datetime.now(et_tz)
        
        # 일광절약시간 확인 (3월 둘째 주 일요일 ~ 11월 첫째 주 일요일)
        dst_start = et_time.replace(month=3, day=8 + (6 - et_time.replace(month=3, day=1).weekday()) % 7, hour=2)
        dst_end = et_time.replace(month=11, day=1 + (6 - et_time.replace(month=11, day=1).weekday()) % 7, hour=2)
        
        if dst_start <= et_time < dst_end:
            et_tz = timezone(timedelta(hours=-4))  # EDT
        
        et_time = datetime.now(et_tz)
        current_time = et_time.time()
        
        # 정규장 시간: 09:30-16:00 ET
        market_open = dtime(9, 30)
        market_close = dtime(16, 0)
        
        is_rth = market_open <= current_time <= market_close
        
        logger.debug(f"RTH 체크: {et_time.strftime('%Y-%m-%d %H:%M:%S %Z')} = {is_rth}")
        return is_rth
    
    def analyze_text(self, text: str, source: str = "", edgar_event: bool = False, regime: str = None, signal_strength: float = 0.0) -> Optional[LLMInsight]:
        """
        텍스트 분석 (호출 조건 제한 적용, Phase 1.5: 강신호 지원)
        
        Args:
            text: 분석할 텍스트 (≤1000자)
            source: 소스 URL (캐시 키용)
            edgar_event: EDGAR 이벤트 여부
            regime: 현재 레짐
            signal_strength: 신호 강도 (0.70+ 강신호)
            
        Returns:
            LLMInsight: 분석 결과 (None if 실패 또는 조건 불충족)
        """
        # LLM 호출 조건 확인 (강신호 지원 추가)
        if not self.should_call_llm(edgar_event, regime, signal_strength):
            logger.debug(f"LLM 호출 스킵: edgar_event={edgar_event}, regime={regime}")
            return None
        
        # 텍스트 길이 제한
        if len(text) > 1000:
            text = text[:1000]
        
        # 캐시 확인
        cache_key = self._generate_cache_key(text, source)
        cached_result = self._get_cached_result(cache_key)
        if cached_result:
            logger.debug(f"캐시 히트: {source}")
            return cached_result
        
        # LLM 비활성화 상태 확인
        if not self.llm_enabled:
            logger.debug("LLM 비활성화 상태 - 캐시된 결과만 사용")
            return None
        
        # 속도 제한 확인
        if not self._check_limits():
            logger.warning("속도 제한 또는 비용 한도 초과")
            return None
        
        # LLM 호출
        try:
            result = self._call_llm(text)
            if result:
                # 캐시에 저장
                self._cache_result(cache_key, result)
                
                # 비용 업데이트
                self._update_cost(result.cost_krw)

                # 메트릭 기록 (Redis HINCRBY/HINCRBYFLOAT)
                try:
                    if self.redis:
                        key = f"metrics:llm:{datetime.utcnow():%Y%m%d}"
                        trigger = "edgar" if edgar_event else (regime or "unknown")
                        self.redis.hincrby(key, "total", 1)
                        self.redis.hincrbyfloat(key, "cost_krw", float(result.cost_krw or 0.0))
                        self.redis.hincrby(key, f"by_trigger:{trigger}", 1)
                except Exception:
                    pass
                
                logger.info(f"LLM 분석 완료: {source} (비용: {result.cost_krw:.0f}원)")
                return result
            
        except Exception as e:
            logger.error(f"LLM 분석 실패: {e}")
        
        return None
    
    def _call_llm(self, text: str) -> Optional[LLMInsight]:
        """LLM API 호출"""
        if not self.client:
            return None
        
        try:
            prompt = self._build_prompt(text)
            
            response = self.client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": "당신은 금융 분석가입니다. 주어진 텍스트를 분석하여 JSON 형태로 응답하세요. 검색하지 말고 텍스트만 해석하세요."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=150,
                temperature=0.3
            )
            
            content = response.choices[0].message.content
            result = self._parse_llm_response(content)
            
            if result:
                # 비용 계산 (GPT-3.5-turbo 기준)
                input_tokens = len(text.split()) * 1.3  # 대략적 추정
                output_tokens = len(content.split())
                cost_usd = (input_tokens * 0.0000015) + (output_tokens * 0.000002)
                cost_krw = cost_usd * self.usd_to_krw
                
                result.cost_krw = cost_krw
                result.timestamp = datetime.now()
            
            return result
            
        except Exception as e:
            logger.error(f"LLM API 호출 실패: {e}")
            return None
    
    def _build_prompt(self, text: str) -> str:
        """프롬프트 구성"""
        return f"""
다음 텍스트를 분석하여 JSON 형태로 응답하세요. 검색하지 말고 텍스트만 해석하세요.

텍스트: {text}

다음 JSON 형식으로만 응답하세요:
{{
    "sentiment": -1.0에서 1.0 사이의 감성 점수 (부정적=-1, 긍정적=+1),
    "trigger": "주요 트리거 키워드나 이벤트",
    "horizon_minutes": 15에서 480 사이의 영향 지속 시간 (분),
    "summary": "한 줄 요약"
}}

예시:
{{
    "sentiment": 0.7,
    "trigger": "실적 예상치 상회",
    "horizon_minutes": 120,
    "summary": "실적 예상치 상회로 긍정적"
}}
"""
    
    def _parse_llm_response(self, response: str) -> Optional[LLMInsight]:
        """LLM 응답 파싱"""
        try:
            # JSON 추출
            start = response.find('{')
            end = response.rfind('}') + 1
            if start == -1 or end == 0:
                return None
            
            json_str = response[start:end]
            data = json.loads(json_str)
            
            # 값 검증 및 클램핑
            sentiment = np.clip(float(data.get("sentiment", 0)), -1.0, 1.0)
            trigger = str(data.get("trigger", ""))
            horizon_minutes = np.clip(int(data.get("horizon_minutes", 120)), 15, 480)
            summary = str(data.get("summary", ""))
            
            return LLMInsight(
                sentiment=sentiment,
                trigger=trigger,
                horizon_minutes=horizon_minutes,
                summary=summary,
                timestamp=datetime.now(),
                cost_krw=0.0  # 나중에 설정
            )
            
        except Exception as e:
            logger.error(f"LLM 응답 파싱 실패: {e}")
            return None
    
    def _generate_cache_key(self, text: str, source: str) -> str:
        """캐시 키 생성"""
        source_hash = hashlib.md5(source.encode()).hexdigest()[:8]
        text_hash = hashlib.md5(text.encode()).hexdigest()[:8]
        return f"{source_hash}_{text_hash}"
    
    def _get_cached_result(self, cache_key: str) -> Optional[LLMInsight]:
        """캐시된 결과 조회"""
        if cache_key in self.cache:
            cached_data = self.cache[cache_key]
            cached_time = datetime.fromisoformat(cached_data["timestamp"])
            
            # 캐시 만료 확인
            if datetime.now() - cached_time < timedelta(hours=self.cache_hours):
                return LLMInsight(
                    sentiment=cached_data["sentiment"],
                    trigger=cached_data["trigger"],
                    horizon_minutes=cached_data["horizon_minutes"],
                    summary=cached_data["summary"],
                    timestamp=cached_time,
                    cost_krw=0.0  # 캐시된 결과는 비용 없음
                )
            else:
                # 만료된 캐시 삭제
                del self.cache[cache_key]
        
        return None
    
    def _cache_result(self, cache_key: str, result: LLMInsight):
        """결과 캐시"""
        self.cache[cache_key] = {
            "sentiment": result.sentiment,
            "trigger": result.trigger,
            "horizon_minutes": result.horizon_minutes,
            "summary": result.summary,
            "timestamp": result.timestamp.isoformat()
        }
    
    def _check_limits(self) -> bool:
        """제한 확인"""
        current_time = time.time()
        
        # 월 비용 확인
        if self.monthly_cost_krw >= self.monthly_cap_krw:
            if self.llm_enabled:
                self._disable_llm()
            return False
        
        # 속도 제한 확인 (분당 10콜로 완화)
        if current_time - self.last_call_time < 6:
            logger.warning("속도 제한: 분당 10콜 초과")
            return False
        
        self.last_call_time = current_time
        return True
    
    def _update_cost(self, cost_krw: float):
        """비용 업데이트"""
        # 월 변경 확인
        current_month = datetime.now().month
        if current_month != self.current_month:
            self.monthly_cost_krw = 0.0
            self.current_month = current_month
        
        self.monthly_cost_krw += cost_krw
        
        # 한도 초과 확인
        if self.monthly_cost_krw >= self.monthly_cap_krw and self.llm_enabled:
            self._disable_llm()
    
    def _disable_llm(self):
        """LLM 비활성화"""
        self.llm_enabled = False
        logger.warning(f"LLM 비활성화: 월 비용 한도 초과 ({self.monthly_cost_krw:,.0f}원)")
        
        # 슬랙 알림
        if self.slack_bot:
            self.slack_bot.send_llm_status_change(False)
    
    def enable_llm(self):
        """LLM 활성화"""
        if not self.llm_enabled:
            self.llm_enabled = True
            logger.info("LLM 활성화됨")
            
            # 슬랙 알림
            if self.slack_bot:
                self.slack_bot.send_llm_status_change(True)
    
    def reset_monthly_cost(self):
        """월 비용 리셋"""
        self.monthly_cost_krw = 0.0
        self.current_month = datetime.now().month
        logger.info("월 비용 리셋됨")
    
    def get_status(self) -> Dict:
        """상태 정보"""
        return {
            "llm_enabled": self.llm_enabled,
            "monthly_cost_krw": self.monthly_cost_krw,
            "monthly_cap_krw": self.monthly_cap_krw,
            "cache_size": len(self.cache),
            "current_month": self.current_month,
            "timestamp": datetime.now().isoformat()
        }
    
    def reset_limits(self):
        """제한 리셋 (테스트용)"""
        self.last_call_time = 0
        logger.info("제한 리셋됨")
    
    def analyze_edgar_filing(self, filing: Dict) -> Optional[LLMInsight]:
        """EDGAR 공시 분석"""
        try:
            ticker = filing.get("ticker", "")
            form_type = filing.get("form_type", "")
            items = filing.get("items", [])
            summary = filing.get("summary", "")
            
            # 분석할 텍스트 구성
            text_parts = [f"Form {form_type}"]
            if items:
                text_parts.append(f"Items: {', '.join(items[:3])}")  # 최대 3개 아이템
            if summary:
                text_parts.append(summary)
            
            text = " | ".join(text_parts)
            
            return self.analyze_text(text, f"edgar_{ticker}_{form_type}", edgar_event=True)
            
        except Exception as e:
            logger.error(f"EDGAR 공시 분석 실패: {e}")
            return None
    
    def analyze_news_headline(self, headline: str, url: str) -> Optional[LLMInsight]:
        """뉴스 헤드라인 분석"""
        return self.analyze_text(headline, url)
