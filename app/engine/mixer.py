"""
시그널 믹서 엔진
레짐별 가중치 + EDGAR 오버라이드 → 최종 시그널
"""
import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .regime import RegimeType, RegimeResult
from .techscore import TechScore
from .llm_insight import LLMInsight

logger = logging.getLogger(__name__)

class SignalType(Enum):
    """시그널 타입"""
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"

@dataclass
class TradingSignal:
    """거래 시그널"""
    ticker: str
    signal_type: SignalType
    score: float  # -1 ~ +1
    confidence: float  # 0~1
    regime: str
    tech_score: float
    sentiment_score: float
    edgar_override: bool
    trigger: str
    summary: str
    entry_price: float
    stop_loss: float
    take_profit: float
    horizon_minutes: int
    timestamp: datetime
    meta: Dict = None

class SignalMixer:
    """시그널 믹서"""
    
    def __init__(self, 
                 buy_threshold: float = 0.6,
                 sell_threshold: float = -0.6,
                 edgar_bonus: float = 0.1):
        """
        Args:
            buy_threshold: 매수 임계값
            sell_threshold: 매도 임계값
            edgar_bonus: EDGAR 오버라이드 보너스
        """
        self.buy_threshold = buy_threshold
        self.sell_threshold = sell_threshold
        self.edgar_bonus = edgar_bonus
        
        # 레짐별 가중치 (기본값)
        self.regime_weights = {
            RegimeType.TREND: {"tech": 0.75, "sentiment": 0.25},
            RegimeType.VOL_SPIKE: {"tech": 0.30, "sentiment": 0.70},
            RegimeType.MEAN_REVERT: {"tech": 0.60, "sentiment": 0.40},
            RegimeType.SIDEWAYS: {"tech": 0.50, "sentiment": 0.50}
        }
        
        logger.info(f"시그널 믹서 초기화: 매수 {buy_threshold}, 매도 {sell_threshold}")
    
    def mix_signals(self, 
                   ticker: str,
                   regime_result: RegimeResult,
                   tech_score: TechScore,
                   llm_insight: Optional[LLMInsight] = None,
                   edgar_filing: Optional[Dict] = None,
                   current_price: float = 0.0) -> Optional[TradingSignal]:
        """
        시그널 믹싱
        
        Args:
            ticker: 종목 코드
            regime_result: 레짐 감지 결과
            tech_score: 기술적 점수
            llm_insight: LLM 인사이트 (선택)
            edgar_filing: EDGAR 공시 (선택)
            current_price: 현재가
            
        Returns:
            TradingSignal: 거래 시그널 (None if 신호 없음)
        """
        # 기본 점수 계산
        tech_score_normalized = tech_score.overall_score
        sentiment_score = 0.0
        
        # LLM 인사이트가 있으면 감성 점수 사용
        if llm_insight:
            sentiment_score = llm_insight.sentiment_score
        elif edgar_filing:
            # EDGAR 공시가 있으면 기본 감성 점수
            sentiment_score = self._get_edgar_sentiment(edgar_filing)
        
        # 레짐별 가중치 적용
        regime = regime_result.regime
        weights = self.regime_weights.get(regime, self.regime_weights[RegimeType.SIDEWAYS])
        
        # 가중 평균 계산
        final_score = (
            tech_score_normalized * weights["tech"] +
            sentiment_score * weights["sentiment"]
        )
        
        # EDGAR 오버라이드 확인
        edgar_override = False
        if edgar_filing and self._is_important_edgar(edgar_filing):
            edgar_override = True
            # EDGAR 보너스 적용 (방향에 따라)
            if sentiment_score > 0:
                final_score += self.edgar_bonus
            else:
                final_score -= self.edgar_bonus
        
        # 신호 타입 결정
        signal_type = self._determine_signal_type(final_score)
        
        # 신호가 없으면 None 반환
        if signal_type == SignalType.HOLD:
            return None
        
        # 신뢰도 계산
        confidence = self._calculate_confidence(
            regime_result.confidence,
            tech_score,
            llm_insight,
            edgar_override
        )
        
        # 진입/손절/익절 가격 계산
        entry_price, stop_loss, take_profit = self._calculate_prices(
            current_price, signal_type, regime
        )
        
        # 트리거와 요약 생성
        trigger, summary = self._generate_trigger_summary(
            ticker, regime, tech_score, llm_insight, edgar_filing
        )
        
        # 호라이즌 결정
        horizon = self._determine_horizon(regime, llm_insight)
        
        signal = TradingSignal(
            ticker=ticker,
            signal_type=signal_type,
            score=final_score,
            confidence=confidence,
            regime=regime.value,
            tech_score=tech_score_normalized,
            sentiment_score=sentiment_score,
            edgar_override=edgar_override,
            trigger=trigger,
            summary=summary,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            horizon_minutes=horizon,
            timestamp=datetime.now(),
            meta={
                "regime_confidence": regime_result.confidence,
                "tech_breakdown": {
                    "trend": tech_score.trend_score,
                    "momentum": tech_score.momentum_score,
                    "volatility": tech_score.volatility_score,
                    "volume": tech_score.volume_score
                }
            }
        )
        
        logger.info(f"시그널 생성: {ticker} {signal_type.value} (점수: {final_score:.2f}, 신뢰도: {confidence:.2f})")
        
        return signal
    
    def _get_edgar_sentiment(self, edgar_filing: Dict) -> float:
        """EDGAR 공시 기본 감성 점수"""
        form_type = edgar_filing.get("form_type", "")
        items = edgar_filing.get("items", [])
        
        # 8-K 중요 아이템별 점수
        if form_type == "8-K":
            item_scores = {
                "2.02": 0.8,  # 실적 발표 (긍정적)
                "1.01": 0.6,  # 중요 계약 (중립~긍정)
                "2.03": 0.3,  # 재무 의무 (중립)
                "2.04": 0.2,  # 트리거 이벤트 (부정적)
                "2.05": 0.1,  # 구조조정 (부정적)
                "2.06": 0.2   # 손상 (부정적)
            }
            
            max_score = 0.0
            for item in items:
                max_score = max(max_score, item_scores.get(item, 0.3))
            
            return max_score
        
        # Form 4 (임원 매매) - 기본 중립
        elif form_type == "4":
            return 0.5
        
        return 0.5  # 기본 중립
    
    def _is_important_edgar(self, edgar_filing: Dict) -> bool:
        """중요한 EDGAR 공시인지 확인"""
        form_type = edgar_filing.get("form_type", "")
        items = edgar_filing.get("items", [])
        
        # 8-K 중요 아이템
        important_items = ["2.02", "1.01", "2.05"]  # 실적, 계약, 구조조정
        
        if form_type == "8-K":
            return any(item in important_items for item in items)
        
        # Form 4는 항상 중요
        elif form_type == "4":
            return True
        
        return False
    
    def _determine_signal_type(self, score: float) -> SignalType:
        """신호 타입 결정"""
        if score >= self.buy_threshold:
            return SignalType.BUY
        elif score <= self.sell_threshold:
            return SignalType.SELL
        else:
            return SignalType.HOLD
    
    def _calculate_confidence(self, 
                            regime_confidence: float,
                            tech_score: TechScore,
                            llm_insight: Optional[LLMInsight],
                            edgar_override: bool) -> float:
        """신뢰도 계산"""
        confidence = 0.0
        weights = 0.0
        
        # 레짐 신뢰도
        confidence += regime_confidence * 0.3
        weights += 0.3
        
        # 기술적 점수 일관성 (각 카테고리 점수의 표준편차가 낮을수록 높은 신뢰도)
        tech_scores = [
            tech_score.trend_score,
            tech_score.momentum_score,
            tech_score.volatility_score,
            tech_score.volume_score
        ]
        tech_consistency = 1.0 - (max(tech_scores) - min(tech_scores))
        confidence += tech_consistency * 0.3
        weights += 0.3
        
        # LLM 신뢰도
        if llm_insight:
            confidence += llm_insight.confidence * 0.2
            weights += 0.2
        
        # EDGAR 오버라이드 보너스
        if edgar_override:
            confidence += 0.2
            weights += 0.2
        
        return confidence / weights if weights > 0 else 0.5
    
    def _calculate_prices(self, 
                         current_price: float,
                         signal_type: SignalType,
                         regime: RegimeType) -> Tuple[float, float, float]:
        """진입/손절/익절 가격 계산"""
        if current_price <= 0:
            return 0.0, 0.0, 0.0
        
        # 레짐별 손익 비율
        regime_ratios = {
            RegimeType.TREND: {"stop_loss": 0.015, "take_profit": 0.03},  # 1.5% / 3%
            RegimeType.VOL_SPIKE: {"stop_loss": 0.02, "take_profit": 0.04},  # 2% / 4%
            RegimeType.MEAN_REVERT: {"stop_loss": 0.01, "take_profit": 0.02},  # 1% / 2%
            RegimeType.SIDEWAYS: {"stop_loss": 0.012, "take_profit": 0.025}  # 1.2% / 2.5%
        }
        
        ratios = regime_ratios.get(regime, regime_ratios[RegimeType.SIDEWAYS])
        
        entry_price = current_price
        
        if signal_type == SignalType.BUY:
            stop_loss = current_price * (1 - ratios["stop_loss"])
            take_profit = current_price * (1 + ratios["take_profit"])
        else:  # SELL
            stop_loss = current_price * (1 + ratios["stop_loss"])
            take_profit = current_price * (1 - ratios["take_profit"])
        
        return entry_price, stop_loss, take_profit
    
    def _generate_trigger_summary(self,
                                ticker: str,
                                regime: RegimeType,
                                tech_score: TechScore,
                                llm_insight: Optional[LLMInsight],
                                edgar_filing: Optional[Dict]) -> Tuple[str, str]:
        """트리거와 요약 생성"""
        trigger_parts = []
        summary_parts = []
        
        # 레짐 기반 트리거
        if regime == RegimeType.TREND:
            trigger_parts.append("추세 돌파")
            summary_parts.append("추세 지속")
        elif regime == RegimeType.VOL_SPIKE:
            trigger_parts.append("변동성 급등")
            summary_parts.append("거래량 급증")
        elif regime == RegimeType.MEAN_REVERT:
            trigger_parts.append("평균회귀")
            summary_parts.append("반등 기대")
        
        # 기술적 지표 기반
        if tech_score.trend_score > 0.7:
            trigger_parts.append("강한 추세")
        elif tech_score.momentum_score > 0.7:
            trigger_parts.append("모멘텀 상승")
        elif tech_score.volume_score > 0.7:
            trigger_parts.append("거래량 급증")
        
        # LLM 인사이트 기반
        if llm_insight:
            trigger_parts.append(llm_insight.trigger)
            summary_parts.append(llm_insight.summary)
        
        # EDGAR 공시 기반
        if edgar_filing:
            form_type = edgar_filing.get("form_type", "")
            if form_type == "8-K":
                trigger_parts.append("8-K 공시")
                summary_parts.append("중요 공시 발표")
            elif form_type == "4":
                trigger_parts.append("Form 4")
                summary_parts.append("임원 매매")
        
        trigger = " + ".join(trigger_parts) if trigger_parts else "기술적 신호"
        summary = " | ".join(summary_parts) if summary_parts else f"{ticker} 거래 신호"
        
        return trigger, summary
    
    def _determine_horizon(self, 
                          regime: RegimeType,
                          llm_insight: Optional[LLMInsight]) -> int:
        """영향 지속 시간 결정 (분)"""
        # LLM 인사이트가 있으면 그 값 사용
        if llm_insight:
            return llm_insight.horizon_minutes
        
        # 레짐별 기본값
        regime_horizons = {
            RegimeType.TREND: 240,  # 4시간
            RegimeType.VOL_SPIKE: 60,  # 1시간
            RegimeType.MEAN_REVERT: 120,  # 2시간
            RegimeType.SIDEWAYS: 180  # 3시간
        }
        
        return regime_horizons.get(regime, 120)
    
    def get_signal_summary(self, signal: TradingSignal) -> Dict:
        """시그널 요약 정보"""
        return {
            "ticker": signal.ticker,
            "signal_type": signal.signal_type.value,
            "score": signal.score,
            "confidence": signal.confidence,
            "regime": signal.regime,
            "tech_score": signal.tech_score,
            "sentiment_score": signal.sentiment_score,
            "edgar_override": signal.edgar_override,
            "trigger": signal.trigger,
            "summary": signal.summary,
            "entry_price": signal.entry_price,
            "stop_loss": signal.stop_loss,
            "take_profit": signal.take_profit,
            "horizon_minutes": signal.horizon_minutes,
            "timestamp": signal.timestamp.isoformat()
        }
