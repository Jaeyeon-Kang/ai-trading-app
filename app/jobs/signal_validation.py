"""
신호 검증 LLM 시스템 (Phase 1.5)
생성된 신호를 LLM으로 재검증하여 품질 향상

기능:
1. 강신호 후속 검증 (0.70+ 신호의 타당성 재확인)
2. 시장 컨텍스트 기반 신호 필터링
3. 리스크 요소 사전 감지
4. 검증 결과 로깅 및 메트릭 수집
"""

import logging
from datetime import datetime
from typing import Optional, Dict, List
from dataclasses import dataclass

from app.engine.mixer import TradingSignal

logger = logging.getLogger(__name__)

@dataclass
class ValidationResult:
    """신호 검증 결과"""
    is_valid: bool
    confidence_adjustment: float  # -0.2 ~ +0.2 신뢰도 조정
    risk_factors: List[str]  # 발견된 리스크 요소들
    validation_reason: str  # 검증 근거
    should_proceed: bool  # 실제 트레이딩 진행 여부
    timestamp: datetime

class SignalValidationEngine:
    """신호 검증 엔진"""
    
    def __init__(self):
        self.validation_enabled = True
        self.min_signal_strength_for_validation = 0.60  # 0.60+ 신호만 검증
        
    def get_trading_components(self):
        """트레이딩 컴포넌트 가져오기"""
        try:
            from app.hooks.autoinit import trading_components
            return trading_components
        except ImportError:
            logger.warning("trading_components를 가져올 수 없음")
            return {}
    
    def should_validate_signal(self, signal: TradingSignal) -> bool:
        """신호 검증이 필요한지 판단"""
        if not self.validation_enabled:
            return False
            
        # 강신호만 검증 (0.60+ 신호)
        if abs(signal.score) >= self.min_signal_strength_for_validation:
            return True
            
        # EDGAR 보너스가 있는 신호도 검증
        if abs(signal.edgar_bonus) > 0:
            return True
            
        return False
    
    def validate_signal(self, signal: TradingSignal) -> Optional[ValidationResult]:
        """신호 검증 수행"""
        if not self.should_validate_signal(signal):
            return None
            
        try:
            components = self.get_trading_components()
            llm_engine = components.get("llm_engine")
            
            if not llm_engine:
                logger.warning("LLM 엔진을 사용할 수 없어 검증 스킵")
                return self._create_fallback_validation(signal)
            
            # LLM을 활용한 신호 검증
            validation_prompt = self._build_validation_prompt(signal)
            
            insight = llm_engine.analyze_text(
                text=validation_prompt,
                source=f"signal_validation_{signal.ticker}",
                edgar_event=False,
                regime=signal.regime,
                signal_strength=abs(signal.score)
            )
            
            if insight and insight.summary:
                return self._parse_validation_result(insight, signal)
            else:
                logger.warning(f"LLM 검증 실패: {signal.ticker}")
                return self._create_fallback_validation(signal)
                
        except Exception as e:
            logger.error(f"신호 검증 오류: {e}")
            return self._create_fallback_validation(signal)
    
    def _build_validation_prompt(self, signal: TradingSignal) -> str:
        """검증용 프롬프트 구성"""
        signal_direction = "매수" if signal.signal_type.value == "long" else "매도"
        
        prompt = f"""
다음 트레이딩 신호를 검증해주세요:

종목: {signal.ticker}
신호: {signal_direction} (점수: {signal.score:.3f})
신뢰도: {signal.confidence:.3f}
시장 레짐: {signal.regime}
트리거: {signal.trigger}
요약: {signal.summary}
진입가: ${signal.entry_price:.2f}
손절가: ${signal.stop_loss:.2f}
익절가: ${signal.take_profit:.2f}

다음 관점에서 검증해주세요:
1. 신호의 타당성 (기술적/펀더멘털 일치성)
2. 현재 시장 상황과의 부합성
3. 잠재적 리스크 요소
4. 리스크/리워드 비율의 적절성

응답 형식:
- 검증결과: VALID/INVALID/CAUTION
- 신뢰도조정: -0.2~+0.2 범위
- 리스크요소: 발견된 리스크들 (쉼표 구분)
- 검증근거: 판단 이유 (한 줄)
- 진행여부: PROCEED/HOLD/REJECT

예시:
검증결과: VALID
신뢰도조정: +0.1  
리스크요소: 고변동성, 낮은거래량
검증근거: 기술적 신호 강하나 변동성 주의 필요
진행여부: PROCEED
"""
        return prompt
    
    def _parse_validation_result(self, insight, signal: TradingSignal) -> ValidationResult:
        """LLM 응답을 ValidationResult로 파싱"""
        summary = insight.summary.lower()
        
        # 검증 결과 파싱
        is_valid = "valid" in summary and "invalid" not in summary
        
        # 신뢰도 조정 파싱
        confidence_adjustment = 0.0
        if "신뢰도조정:" in summary:
            try:
                adj_part = summary.split("신뢰도조정:")[1].split()[0]
                confidence_adjustment = max(-0.2, min(0.2, float(adj_part)))
            except Exception:
                pass
        
        # 리스크 요소 파싱
        risk_factors = []
        if "리스크요소:" in summary:
            try:
                risk_part = summary.split("리스크요소:")[1].split("검증근거:")[0]
                risk_factors = [r.strip() for r in risk_part.split(",") if r.strip()]
            except Exception:
                pass
        
        # 진행 여부 파싱
        should_proceed = True
        if "진행여부:" in summary:
            proceed_part = summary.split("진행여부:")[1].split()[0]
            should_proceed = proceed_part in ["proceed", "진행"]
        elif "reject" in summary or "hold" in summary:
            should_proceed = False
        
        return ValidationResult(
            is_valid=is_valid,
            confidence_adjustment=confidence_adjustment,
            risk_factors=risk_factors,
            validation_reason=insight.summary,
            should_proceed=should_proceed,
            timestamp=datetime.now()
        )
    
    def _create_fallback_validation(self, signal: TradingSignal) -> ValidationResult:
        """LLM 없을 때 기본 검증"""
        # 기본적으로 강신호는 통과, 약신호는 주의
        is_valid = abs(signal.score) >= 0.70
        confidence_adjustment = 0.1 if is_valid else -0.1
        
        risk_factors = []
        if signal.regime == "vol_spike":
            risk_factors.append("고변동성")
        if signal.confidence < 0.6:
            risk_factors.append("낮은신뢰도")
        
        return ValidationResult(
            is_valid=is_valid,
            confidence_adjustment=confidence_adjustment,
            risk_factors=risk_factors,
            validation_reason="기본 검증 (LLM 없음)",
            should_proceed=is_valid,
            timestamp=datetime.now()
        )
    
    def apply_validation_result(self, signal: TradingSignal, validation: ValidationResult) -> TradingSignal:
        """검증 결과를 신호에 반영"""
        # 신뢰도 조정 적용
        adjusted_confidence = max(0.0, min(1.0, signal.confidence + validation.confidence_adjustment))
        
        # 메타데이터에 검증 정보 추가
        if not signal.meta:
            signal.meta = {}
        
        signal.meta.update({
            "validation": {
                "is_valid": validation.is_valid,
                "confidence_adjustment": validation.confidence_adjustment,
                "risk_factors": validation.risk_factors,
                "validation_reason": validation.validation_reason,
                "should_proceed": validation.should_proceed,
                "validated_at": validation.timestamp.isoformat()
            },
            "original_confidence": signal.confidence
        })
        
        # 신뢰도 업데이트
        signal.confidence = adjusted_confidence
        
        # 요약에 검증 결과 추가
        if validation.risk_factors:
            risk_text = ", ".join(validation.risk_factors[:2])  # 최대 2개만
            signal.summary += f" (검증: {risk_text})"
        
        logger.info(f"신호 검증 완료: {signal.ticker} - {'✅' if validation.should_proceed else '⚠️'}")
        
        return signal
    
    def get_validation_stats(self) -> Dict:
        """검증 통계 (Redis에서 조회)"""
        try:
            components = self.get_trading_components()
            redis_client = components.get("redis_streams")
            
            if not redis_client:
                return {}
            
            # 오늘 검증 통계
            today = datetime.now().strftime("%Y%m%d")
            key = f"validation_stats:{today}"
            
            stats = redis_client.hgetall(key)
            return {k.decode(): v.decode() for k, v in stats.items()}
            
        except Exception as e:
            logger.error(f"검증 통계 조회 실패: {e}")
            return {}
    
    def log_validation_metrics(self, signal: TradingSignal, validation: ValidationResult):
        """검증 메트릭 로깅"""
        try:
            components = self.get_trading_components()
            redis_client = components.get("redis_streams")
            
            if not redis_client:
                return
            
            # 일일 통계 업데이트
            today = datetime.now().strftime("%Y%m%d")
            key = f"validation_stats:{today}"
            
            redis_client.hincrby(key, "total_validations", 1)
            
            if validation.is_valid:
                redis_client.hincrby(key, "valid_signals", 1)
            else:
                redis_client.hincrby(key, "invalid_signals", 1)
            
            if validation.should_proceed:
                redis_client.hincrby(key, "proceed_signals", 1)
            else:
                redis_client.hincrby(key, "rejected_signals", 1)
            
            # 리스크 요소별 카운트
            for risk in validation.risk_factors:
                redis_client.hincrby(key, f"risk:{risk}", 1)
            
            # TTL 설정 (7일)
            redis_client.expire(key, 7 * 24 * 3600)
            
        except Exception as e:
            logger.error(f"검증 메트릭 로깅 실패: {e}")


# =============================================================================
# Celery Tasks
# =============================================================================

# scheduler.py에서 celery_app import
try:
    from app.jobs.scheduler import celery_app
    CELERY_AVAILABLE = True
except ImportError:
    celery_app = None
    CELERY_AVAILABLE = False
    logger.warning("Celery app을 임포트할 수 없음 (테스트 환경일 수 있음)")

if CELERY_AVAILABLE and celery_app:
    @celery_app.task(name="app.jobs.signal_validation.validate_and_process_signal")
    def validate_and_process_signal(signal_data: Dict):
        """신호 검증 및 처리 태스크"""
        try:
            # TradingSignal 객체 재구성
            from app.engine.mixer import TradingSignal, SignalType
            
            signal = TradingSignal(
                ticker=signal_data["ticker"],
                signal_type=SignalType(signal_data["signal_type"]),
                score=signal_data["score"],
                confidence=signal_data["confidence"],
                regime=signal_data["regime"],
                tech_score=signal_data["tech_score"],
                sentiment_score=signal_data["sentiment_score"],
                edgar_bonus=signal_data["edgar_bonus"],
                trigger=signal_data["trigger"],
                summary=signal_data["summary"],
                entry_price=signal_data["entry_price"],
                stop_loss=signal_data["stop_loss"],
                take_profit=signal_data["take_profit"],
                horizon_minutes=signal_data["horizon_minutes"],
                timestamp=datetime.fromisoformat(signal_data["timestamp"]),
                meta=signal_data.get("meta", {})
            )
            
            # 검증 수행
            engine = SignalValidationEngine()
            validation = engine.validate_signal(signal)
            
            if validation:
                # 검증 결과 적용
                validated_signal = engine.apply_validation_result(signal, validation)
                
                # 메트릭 로깅
                engine.log_validation_metrics(validated_signal, validation)
                
                # 검증 통과한 신호만 실제 처리
                if validation.should_proceed:
                    logger.info(f"✅ 신호 검증 통과: {signal.ticker}")
                    return {"status": "validated_and_processed", "signal": validated_signal.get_summary()}
                else:
                    logger.info(f"🛑 신호 검증 거부: {signal.ticker}")
                    return {"status": "validated_but_rejected", "reason": validation.validation_reason}
            else:
                logger.info(f"⏭️ 검증 스킵: {signal.ticker}")
                return {"status": "validation_skipped"}
                
        except Exception as e:
            logger.error(f"신호 검증 태스크 오류: {e}")
            return {"status": "validation_error", "error": str(e)}

else:
    # Celery가 없을 때 fallback
    def validate_and_process_signal(signal_data: Dict):
        """Celery 없을 때 fallback"""
        logger.warning("Celery 없이 신호 검증 호출됨")
        # 기본 검증만 수행
        return {"status": "fallback_validation"}