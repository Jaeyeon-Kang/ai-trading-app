"""
Slack Bot
알림 + 버튼 승인 기능
"""
import requests
import json
import logging
from typing import Dict, List, Optional, Callable
from datetime import datetime
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

@dataclass
class SlackMessage:
    """Slack 메시지"""
    channel: str
    text: str
    blocks: List[Dict] = None
    attachments: List[Dict] = None
    thread_ts: str = None

@dataclass
class SignalNotification:
    """시그널 알림"""
    ticker: str
    signal_type: str  # "buy", "sell"
    score: float
    confidence: float
    trigger: str
    summary: str
    entry_price: float
    stop_loss: float
    take_profit: float
    timestamp: datetime

class SlackBot:
    """Slack Bot"""
    
    def __init__(self, token: str, channel: str = "#trading-signals"):
        """
        Args:
            token: Slack Bot Token
            channel: 기본 채널
        """
        self.token = token
        self.default_channel = channel
        self.base_url = "https://slack.com/api"
        
        # 승인 콜백 저장
        self.approval_callbacks: Dict[str, Callable] = {}
        
        # 메시지 템플릿
        self.templates = {
            "signal": {
                "buy": {
                    "color": "#36a64f",  # 초록색
                    "emoji": "🟢"
                },
                "sell": {
                    "color": "#ff0000",  # 빨간색
                    "emoji": "🔴"
                }
            }
        }
        
        logger.info(f"Slack Bot 초기화: 채널 {channel}")
    
    def send_message(self, message: SlackMessage) -> bool:
        """메시지 전송"""
        payload = {
            "channel": message.channel,
            "text": message.text
        }
        
        if message.blocks:
            payload["blocks"] = message.blocks
        
        if message.attachments:
            payload["attachments"] = message.attachments
        
        if message.thread_ts:
            payload["thread_ts"] = message.thread_ts
        
        try:
            response = requests.post(
                f"{self.base_url}/chat.postMessage",
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json"
                },
                json=payload
            )
            
            response.raise_for_status()
            result = response.json()
            
            if result.get("ok"):
                logger.debug(f"Slack 메시지 전송 성공: {message.channel}")
                return True
            else:
                logger.error(f"Slack 메시지 전송 실패: {result.get('error')}")
                return False
                
        except Exception as e:
            logger.error(f"Slack 메시지 전송 실패: {e}")
            return False
    
    def send_signal_notification(self, signal: SignalNotification) -> str:
        """거래 시그널 알림 전송"""
        signal_config = self.templates["signal"][signal.signal_type]
        
        # 메시지 텍스트
        text = f"{signal_config['emoji']} *{signal.ticker} {signal.signal_type.upper()}* 시그널"
        
        # 블록 구성
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"{signal.ticker} {signal.signal_type.upper()} 시그널"
                }
            },
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*점수:* {signal.score:.2f}"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*신뢰도:* {signal.confidence:.1%}"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*진입가:* ${signal.entry_price:.2f}"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*손절가:* ${signal.stop_loss:.2f}"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*익절가:* ${signal.take_profit:.2f}"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*시간:* {signal.timestamp.strftime('%H:%M:%S')}"
                    }
                ]
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*트리거:* {signal.trigger}\n*요약:* {signal.summary}"
                }
            }
        ]
        
        # 승인 버튼 (반자동 모드에서만)
        if self._is_semi_auto_mode():
            blocks.append({
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "✅ 매수",
                            "emoji": True
                        },
                        "style": "primary",
                        "value": f"approve_{signal.ticker}_{signal.signal_type}_{signal.timestamp.timestamp()}",
                        "action_id": "approve_trade"
                    },
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "❌ 패스",
                            "emoji": True
                        },
                        "style": "danger",
                        "value": f"reject_{signal.ticker}_{signal.signal_type}_{signal.timestamp.timestamp()}",
                        "action_id": "reject_trade"
                    }
                ]
            })
        
        # 첨부파일 (색상)
        attachments = [
            {
                "color": signal_config["color"],
                "footer": "Trading Bot",
                "ts": int(signal.timestamp.timestamp())
            }
        ]
        
        message = SlackMessage(
            channel=self.default_channel,
            text=text,
            blocks=blocks,
            attachments=attachments
        )
        
        success = self.send_message(message)
        if success:
            # 승인 콜백 등록
            callback_id = f"{signal.ticker}_{signal.signal_type}_{signal.timestamp.timestamp()}"
            self.approval_callbacks[callback_id] = {
                "signal": signal,
                "timestamp": datetime.now()
            }
            
            logger.info(f"시그널 알림 전송: {signal.ticker} {signal.signal_type}")
            return callback_id
        else:
            logger.error(f"시그널 알림 전송 실패: {signal.ticker}")
            return ""
    
    def send_daily_report(self, report_data: Dict) -> bool:
        """일일 리포트 전송"""
        text = "📊 *일일 거래 리포트*"
        
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "일일 거래 리포트"
                }
            },
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*거래 수:* {report_data.get('trades', 0)}건"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*승률:* {report_data.get('win_rate', 0):.1%}"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*실현손익:* ${report_data.get('realized_pnl', 0):,.2f}"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*평균 R/R:* {report_data.get('avg_rr', 0):.2f}"
                    }
                ]
            }
        ]
        
        # 리스크 지표
        if "risk_metrics" in report_data:
            risk = report_data["risk_metrics"]
            blocks.append({
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*VaR 95%:* {risk.get('var_95', 0):.2%}"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*최대 드로다운:* {risk.get('max_drawdown', 0):.2%}"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*포지션 수:* {risk.get('position_count', 0)}개"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*총 노출:* {risk.get('exposure_pct', 0):.1%}"
                    }
                ]
            })
        
        # LLM 사용량
        if "llm_usage" in report_data:
            llm = report_data["llm_usage"]
            blocks.append({
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*LLM 호출:* {llm.get('daily_calls', 0)}회"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*LLM 비용:* ${llm.get('monthly_cost_usd', 0):.2f}"
                    }
                ]
            })
        
        message = SlackMessage(
            channel=self.default_channel,
            text=text,
            blocks=blocks
        )
        
        success = self.send_message(message)
        if success:
            logger.info("일일 리포트 전송 완료")
        else:
            logger.error("일일 리포트 전송 실패")
        
        return success
    
    def send_risk_alert(self, risk_data: Dict) -> bool:
        """리스크 알림 전송"""
        status = risk_data.get("status", "normal")
        
        if status == "warning":
            emoji = "⚠️"
            color = "#ffa500"  # 주황색
            title = "리스크 경고"
        elif status == "critical":
            emoji = "🚨"
            color = "#ff0000"  # 빨간색
            title = "리스크 위험"
        elif status == "shutdown":
            emoji = "🛑"
            color = "#800000"  # 진한 빨간색
            title = "리스크 셧다운"
        else:
            return True  # normal 상태는 알림 안 보냄
        
        text = f"{emoji} *{title}*"
        
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": title
                }
            },
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*일일 손익:* {risk_data.get('daily_pnl_pct', 0):.2%}"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*VaR 95%:* {risk_data.get('var_95', 0):.2%}"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*포지션 수:* {risk_data.get('position_count', 0)}개"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*총 노출:* {risk_data.get('exposure_pct', 0):.1%}"
                    }
                ]
            }
        ]
        
        # 셧다운 사유
        if status == "shutdown" and "shutdown_reason" in risk_data:
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*셧다운 사유:* {risk_data['shutdown_reason']}"
                }
            })
        
        # 긴급 조치 버튼
        if status in ["critical", "shutdown"]:
            blocks.append({
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "🛑 긴급 중지",
                            "emoji": True
                        },
                        "style": "danger",
                        "value": "emergency_stop",
                        "action_id": "emergency_stop"
                    }
                ]
            })
        
        attachments = [
            {
                "color": color,
                "footer": "Trading Bot",
                "ts": int(datetime.now().timestamp())
            }
        ]
        
        message = SlackMessage(
            channel=self.default_channel,
            text=text,
            blocks=blocks,
            attachments=attachments
        )
        
        success = self.send_message(message)
        if success:
            logger.warning(f"리스크 알림 전송: {status}")
        else:
            logger.error(f"리스크 알림 전송 실패: {status}")
        
        return success
    
    def send_system_status(self, status_data: Dict) -> bool:
        """시스템 상태 알림"""
        status = status_data.get("status", "unknown")
        
        if status == "healthy":
            emoji = "✅"
            color = "#36a64f"
        else:
            emoji = "❌"
            color = "#ff0000"
        
        text = f"{emoji} *시스템 상태: {status}*"
        
        blocks = [
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*Redis:* {'연결됨' if status_data.get('redis_connected') else '연결 안됨'}"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*LLM:* {status_data.get('llm_status', 'unknown')}"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*시그널 지연:* {status_data.get('signal_latency_ms', 0)}ms"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*체결 지연:* {status_data.get('execution_latency_ms', 0)}ms"
                    }
                ]
            }
        ]
        
        attachments = [
            {
                "color": color,
                "footer": "Trading Bot",
                "ts": int(datetime.now().timestamp())
            }
        ]
        
        message = SlackMessage(
            channel=self.default_channel,
            text=text,
            blocks=blocks,
            attachments=attachments
        )
        
        return self.send_message(message)
    
    def handle_interaction(self, payload: Dict) -> Dict:
        """상호작용 처리 (버튼 클릭 등)"""
        try:
            if payload.get("type") == "block_actions":
                for action in payload.get("actions", []):
                    action_id = action.get("action_id")
                    value = action.get("value", "")
                    
                    if action_id == "approve_trade":
                        return self._handle_trade_approval(value, True)
                    elif action_id == "reject_trade":
                        return self._handle_trade_approval(value, False)
                    elif action_id == "emergency_stop":
                        return self._handle_emergency_stop()
            
            return {"status": "ignored"}
            
        except Exception as e:
            logger.error(f"상호작용 처리 실패: {e}")
            return {"status": "error", "message": str(e)}
    
    def _handle_trade_approval(self, value: str, approved: bool) -> Dict:
        """거래 승인/거부 처리"""
        try:
            # value 형식: "approve_TICKER_SIGNAL_TYPE_TIMESTAMP"
            parts = value.split("_")
            if len(parts) >= 4:
                ticker = parts[1]
                signal_type = parts[2]
                timestamp = parts[3]
                
                callback_id = f"{ticker}_{signal_type}_{timestamp}"
                
                if callback_id in self.approval_callbacks:
                    callback_data = self.approval_callbacks[callback_id]
                    signal = callback_data["signal"]
                    
                    # 승인/거부 메시지 전송
                    status = "승인" if approved else "거부"
                    emoji = "✅" if approved else "❌"
                    
                    text = f"{emoji} *{signal.ticker} {signal.signal_type.upper()} {status}*"
                    
                    blocks = [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"{signal.ticker} {signal.signal_type.upper()} 거래가 {status}되었습니다."
                            }
                        }
                    ]
                    
                    message = SlackMessage(
                        channel=self.default_channel,
                        text=text,
                        blocks=blocks
                    )
                    
                    self.send_message(message)
                    
                    # 콜백 제거
                    del self.approval_callbacks[callback_id]
                    
                    logger.info(f"거래 {status}: {ticker} {signal_type}")
                    
                    return {
                        "status": "success",
                        "action": "trade_approval",
                        "approved": approved,
                        "ticker": ticker,
                        "signal_type": signal_type
                    }
        
        except Exception as e:
            logger.error(f"거래 승인 처리 실패: {e}")
        
        return {"status": "error", "message": "거래 승인 처리 실패"}
    
    def _handle_emergency_stop(self) -> Dict:
        """긴급 중지 처리"""
        text = "🛑 *긴급 중지 요청됨*"
        
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "사용자가 긴급 중지를 요청했습니다. 모든 거래가 중지됩니다."
                }
            }
        ]
        
        message = SlackMessage(
            channel=self.default_channel,
            text=text,
            blocks=blocks
        )
        
        self.send_message(message)
        
        logger.warning("긴급 중지 요청됨")
        
        return {
            "status": "success",
            "action": "emergency_stop"
        }
    
    def _is_semi_auto_mode(self) -> bool:
        """반자동 모드 여부 확인"""
        # 환경변수나 설정에서 확인
        # 여기서는 간단히 True 반환
        return True
    
    def cleanup_old_callbacks(self, max_age_hours: int = 24):
        """오래된 콜백 정리"""
        cutoff_time = datetime.now().timestamp() - (max_age_hours * 3600)
        
        expired_keys = []
        for callback_id, callback_data in self.approval_callbacks.items():
            if callback_data["timestamp"].timestamp() < cutoff_time:
                expired_keys.append(callback_id)
        
        for key in expired_keys:
            del self.approval_callbacks[key]
        
        if expired_keys:
            logger.info(f"오래된 콜백 {len(expired_keys)}개 정리 완료")
    
    def get_status(self) -> Dict:
        """상태 정보"""
        return {
            "connected": True,  # 실제로는 연결 상태 확인 필요
            "channel": self.default_channel,
            "pending_callbacks": len(self.approval_callbacks),
            "timestamp": datetime.now().isoformat()
        }
