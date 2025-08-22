"""
일일 시장 브리핑 시스템 (Phase 1.5)
답답함 해소를 위한 능동적 LLM 소통 시스템

기능:
1. 정기 브리핑 (아침/점심/저녁)
2. 조용한 시장 설명 ("오늘은 추천드릴게 없네요")
3. 시장 상황 분석 및 예측
"""

import logging
import os
import sys
from datetime import datetime
from typing import Optional
import redis


logger = logging.getLogger(__name__)

class DailyBriefingEngine:
    """일일 브리핑 엔진"""
    
    def __init__(self):
        self.redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")
        self.quiet_threshold_hours = 3  # 3시간 이상 조용하면 설명 메시지
        
    def get_trading_components(self):
        """트레이딩 컴포넌트 가져오기"""
        try:
            from app.hooks.autoinit import trading_components
            return trading_components
        except ImportError:
            logger.warning("trading_components를 가져올 수 없음")
            return {}
    
    def get_last_signal_time(self) -> Optional[datetime]:
        """마지막 신호 시간 확인"""
        try:
            r = redis.from_url(self.redis_url)
            # 최근 신호 리스트에서 마지막 시간 확인
            signals = r.lrange("recent_signals", 0, 0)  # 가장 최근 1개
            if signals:
                import json
                signal_data = json.loads(signals[0])
                return datetime.fromisoformat(signal_data.get("timestamp", ""))
        except Exception as e:
            logger.warning(f"마지막 신호 시간 확인 실패: {e}")
        
        return None
    
    def should_send_quiet_market_message(self) -> bool:
        """조용한 시장 메시지를 보낼지 판단"""
        last_signal_time = self.get_last_signal_time()
        
        if not last_signal_time:
            # 신호가 전혀 없었으면 조용한 상태
            return True
            
        time_diff = datetime.now() - last_signal_time
        hours_passed = time_diff.total_seconds() / 3600
        
        return hours_passed >= self.quiet_threshold_hours
    
    def generate_quiet_market_briefing(self) -> str:
        """조용한 시장 브리핑 생성 - 답답함 해소의 핵심!"""
        
        components = self.get_trading_components()
        llm_engine = components.get("llm_engine")
        
        if not llm_engine:
            # LLM이 없으면 기본 메시지
            return """😊 안녕하세요! 

오늘은 추천드릴 만한 트레이딩 기회가 보이지 않네요.

시장이 비교적 안정적이거나 명확한 방향성이 없어서 
기다리는 게 현명한 선택일 것 같아요.

이런 때일수록 차분히 다음 기회를 기다려보시면 좋겠습니다! 💪

좋은 기회가 생기면 바로 알려드릴게요 🎯"""
        
        # LLM을 활용한 상세 브리핑
        try:
            last_signal_time = self.get_last_signal_time()
            hours_quiet = 3 if not last_signal_time else (datetime.now() - last_signal_time).total_seconds() / 3600
            
            prompt = f"""
시장이 {hours_quiet:.1f}시간 동안 조용합니다. 사용자에게 친근하고 격려적인 메시지를 작성해주세요.

포함할 내용:
1. "오늘은 추천드릴 만한 기회가 없네요" 느낌으로 시작
2. 왜 조용한지 간단한 설명 (횡보, 대기, 불확실성 등)
3. 이런 때 투자자가 어떻게 대응하면 좋은지 조언
4. 다음 기회에 대한 희망적 메시지
5. 친근하고 따뜻한 톤으로 작성

길이: 4-6줄 정도로 간결하게, 이모지도 적절히 사용해주세요.
"""
            
            insight = llm_engine.analyze_text(
                text=prompt,
                source="quiet_market_briefing",
                edgar_event=False,
                regime=None,
                signal_strength=0.0
            )
            
            if insight and insight.summary:
                return f"💭 **시장 브리핑**\n\n{insight.summary}"
            
        except Exception as e:
            logger.warning(f"LLM 조용한 시장 브리핑 생성 실패: {e}")
        
        # 폴백 메시지
        return """💭 **시장 브리핑**

😊 안녕하세요! 오늘은 추천드릴 만한 기회가 보이지 않네요.

시장이 비교적 안정적이어서 명확한 트레이딩 신호가 나오지 않고 있어요. 
이런 때일수록 차분히 기다리는 것이 현명한 전략입니다.

좋은 기회가 생기면 바로 알려드릴게요! 🎯"""
    
    def generate_scheduled_briefing(self, briefing_type: str) -> str:
        """정기 브리핑 생성 (아침/점심/저녁)"""
        
        components = self.get_trading_components()
        llm_engine = components.get("llm_engine")
        
        # 브리핑 타입별 기본 메시지
        default_messages = {
            "morning": """🌅 **좋은 아침이에요!**
            
오늘도 시장을 주의깊게 지켜보고 있어요.
좋은 기회가 있으면 바로 알려드릴게요! 

즐거운 하루 보내세요 😊""",
            
            "midday": """🍽️ **점심시간 브리핑**
            
오전 시장 상황을 정리해드릴게요.
오후에도 계속 모니터링하고 있으니 안심하세요!

맛있는 점심 드세요 😋""",
            
            "evening": """🌆 **오늘 마감 브리핑**
            
오늘 하루 시장 상황을 정리해드릴게요.
내일도 좋은 기회를 찾아보겠습니다!

편안한 저녁 보내세요 🌙"""
        }
        
        if not llm_engine:
            return default_messages.get(briefing_type, default_messages["morning"])
        
        # LLM을 활용한 상세 브리핑
        try:
            time_context = {
                "morning": "아침 시장 오픈 전",
                "midday": "점심시간, 오전 시장 정리",
                "evening": "시장 마감 후, 하루 총정리"
            }
            
            prompt = f"""
{time_context[briefing_type]} 브리핑을 친근하게 작성해주세요.

포함할 내용:
1. 시간대에 맞는 인사말
2. 현재 시장 상황이나 분위기
3. 투자자에게 도움이 되는 간단한 조언
4. 격려와 희망적인 메시지

톤: 친근하고 따뜻하며 전문적
길이: 3-5줄 정도로 간결하게
이모지 적절히 사용
"""
            
            insight = llm_engine.analyze_text(
                text=prompt,
                source=f"{briefing_type}_briefing",
                edgar_event=False,
                regime=None,
                signal_strength=0.0
            )
            
            if insight and insight.summary:
                icon = {"morning": "🌅", "midday": "🍽️", "evening": "🌆"}[briefing_type]
                return f"{icon} **{briefing_type.title()} 브리핑**\n\n{insight.summary}"
                
        except Exception as e:
            logger.warning(f"LLM {briefing_type} 브리핑 생성 실패: {e}")
        
        return default_messages.get(briefing_type, default_messages["morning"])
    
    def send_briefing_to_slack(self, message: str) -> bool:
        """브리핑을 Slack으로 전송"""
        try:
            components = self.get_trading_components()
            slack_bot = components.get("slack_bot")
            
            if not slack_bot:
                logger.warning("Slack bot을 사용할 수 없음")
                return False
            
            channel_id = os.getenv("SLACK_CHANNEL_ID")
            slack_message = {"text": message}
            if channel_id:
                slack_message["channel"] = channel_id
            else:
                logger.warning("SLACK_CHANNEL_ID 미설정 - SlackBot 기본 채널 사용 시도")
            
            result = slack_bot.send_message(slack_message)
            if result:
                logger.info("✅ 브리핑 Slack 전송 성공")
                return True
            else:
                logger.warning("❌ 브리핑 Slack 전송 실패")
                return False
                
        except Exception as e:
            logger.error(f"브리핑 Slack 전송 오류: {e}")
            return False

# =============================================================================
# Celery Tasks
# =============================================================================

# Celery import는 scheduler.py에서 가져옴
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# scheduler.py에서 celery_app import
try:
    from app.jobs.scheduler import celery_app
    CELERY_AVAILABLE = True
except ImportError:
    # 테스트 환경에서는 None으로 설정
    celery_app = None
    CELERY_AVAILABLE = False
    logger.warning("Celery app을 임포트할 수 없음 (테스트 환경일 수 있음)")

# Celery가 사용 가능할 때만 태스크 정의
if CELERY_AVAILABLE and celery_app:
    @celery_app.task(name="app.jobs.daily_briefing.send_scheduled_briefing")
    def send_scheduled_briefing(briefing_type: str = "morning"):
        """정기 브리핑 전송 태스크"""
        try:
            logger.info(f"📊 {briefing_type} 브리핑 시작")
            
            engine = DailyBriefingEngine()
            message = engine.generate_scheduled_briefing(briefing_type)
            result = engine.send_briefing_to_slack(message)
            
            if result:
                logger.info(f"✅ {briefing_type} 브리핑 완료")
                return {"status": "success", "type": briefing_type}
            else:
                logger.warning(f"⚠️ {briefing_type} 브리핑 전송 실패")
                return {"status": "failed", "type": briefing_type}
                
        except Exception as e:
            logger.error(f"❌ {briefing_type} 브리핑 오류: {e}")
            return {"status": "error", "type": briefing_type, "error": str(e)}

    @celery_app.task(name="app.jobs.daily_briefing.send_quiet_market_message")
    def send_quiet_market_message():
        """조용한 시장 메시지 전송 태스크 - 답답함 해소의 핵심!"""
        try:
            logger.info("🔍 조용한 시장 체크 시작")
            
            engine = DailyBriefingEngine()
            
            if engine.should_send_quiet_market_message():
                logger.info("💬 조용한 시장 감지 - 설명 메시지 전송")
                message = engine.generate_quiet_market_briefing()
                result = engine.send_briefing_to_slack(message)
                
                if result:
                    logger.info("✅ 조용한 시장 메시지 전송 완료")
                    return {"status": "sent", "reason": "quiet_market"}
                else:
                    logger.warning("⚠️ 조용한 시장 메시지 전송 실패")
                    return {"status": "failed", "reason": "slack_error"}
            else:
                logger.info("🎯 최근에 신호가 있었음 - 메시지 전송하지 않음")
                return {"status": "skipped", "reason": "recent_signal"}
                
        except Exception as e:
            logger.error(f"❌ 조용한 시장 메시지 오류: {e}")
            return {"status": "error", "error": str(e)}

    @celery_app.task(name="app.jobs.daily_briefing.check_and_send_quiet_message")
    def check_and_send_quiet_message():
        """조용한 시장 체크 및 메시지 전송 (정기 실행용)"""
        try:
            DailyBriefingEngine()
            
            # 현재 시간이 적절한지 체크 (9시-18시 사이에만)
            current_hour = datetime.now().hour
            if current_hour < 9 or current_hour > 18:
                logger.info(f"⏰ 브리핑 시간이 아님 (현재: {current_hour}시)")
                return {"status": "skipped", "reason": "outside_hours"}
            
            return send_quiet_market_message.delay().get()
            
        except Exception as e:
            logger.error(f"❌ 조용한 시장 체크 오류: {e}")
            return {"status": "error", "error": str(e)}

else:
    # Celery가 없을 때를 위한 fallback 함수들
    def send_scheduled_briefing(briefing_type: str = "morning"):
        """Celery 없을 때 fallback"""
        logger.warning("Celery 없이 브리핑 함수 호출됨")
        engine = DailyBriefingEngine()
        message = engine.generate_scheduled_briefing(briefing_type)
        return engine.send_briefing_to_slack(message)
    
    def send_quiet_market_message():
        """Celery 없을 때 fallback"""
        logger.warning("Celery 없이 조용한 시장 메시지 호출됨")
        engine = DailyBriefingEngine()
        if engine.should_send_quiet_market_message():
            message = engine.generate_quiet_market_briefing()
            return engine.send_briefing_to_slack(message)
        return False
    
    def check_and_send_quiet_message():
        """Celery 없을 때 fallback"""
        logger.warning("Celery 없이 조용한 시장 체크 호출됨")
        return send_quiet_market_message()
