"""
Redis Streams publish/consume
메시지 버스로 사용할 Redis Streams 기본 기능
"""
import redis
import json
import logging
from typing import Dict, List, Optional, Any
from datetime import datetime
import time
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)

@dataclass
class StreamMessage:
    """스트림 메시지"""
    stream: str
    message_id: str
    data: Dict[str, Any]
    timestamp: datetime

class RedisStreams:
    """Redis Streams 관리"""
    
    def __init__(self, host: str = "localhost", port: int = 6379, db: int = 0):
        """
        Args:
            host: Redis 호스트
            port: Redis 포트
            db: Redis 데이터베이스 번호
        """
        self.redis_client = redis.Redis(host=host, port=port, db=db, decode_responses=True)
        self.consumer_group = "bot"  # 요구사항에 맞게 변경
        self.consumer_name = "worker_1"
        
        # 스트림 키 정의
        self.streams = {
            "quotes": "quotes.{MIC}.{TICKER}",
            "news": "news.headlines",
            "edgar": "news.edgar", 
            "signals": "signals.raw",
            "tradable": "signals.tradable",
            "orders": "orders.submitted",
            "fills": "orders.fills",
            "risk": "risk.pnl"
        }
        
        logger.info(f"Redis Streams 초기화: {host}:{port}")
    
    def publish_quote(self, ticker: str, mic: str, data: Dict):
        """시세 데이터 발행"""
        stream_key = f"quotes.{mic}.{ticker}"
        message = {
            "ticker": ticker,
            "mic": mic,
            "timestamp": datetime.now().isoformat(),
            **data
        }
        
        try:
            message_id = self.redis_client.xadd(stream_key, message)
            logger.debug(f"시세 발행: {stream_key} -> {message_id}")
            return message_id
        except Exception as e:
            logger.error(f"시세 발행 실패: {e}")
            return None
    
    def publish_news(self, data: Dict):
        """뉴스 헤드라인 발행"""
        message = {
            "timestamp": datetime.now().isoformat(),
            **data
        }
        
        try:
            message_id = self.redis_client.xadd("news.headlines", message)
            logger.debug(f"뉴스 발행: {message_id}")
            return message_id
        except Exception as e:
            logger.error(f"뉴스 발행 실패: {e}")
            return None
    
    def publish_edgar(self, data: Dict):
        """EDGAR 공시 발행"""
        message = {
            "timestamp": datetime.now().isoformat(),
            **data
        }
        
        try:
            message_id = self.redis_client.xadd("news.edgar", message)
            logger.debug(f"EDGAR 발행: {message_id}")
            return message_id
        except Exception as e:
            logger.error(f"EDGAR 발행 실패: {e}")
            return None
    
    def publish_signal(self, signal_data: Dict):
        """거래 시그널 발행"""
        message = {
            "timestamp": datetime.now().isoformat(),
            **signal_data
        }
        
        try:
            message_id = self.redis_client.xadd("signals.raw", message)
            logger.info(f"시그널 발행: {message_id}")
            return message_id
        except Exception as e:
            logger.error(f"시그널 발행 실패: {e}")
            return None
    
    def publish_tradable_signal(self, signal_data: Dict):
        """거래 가능한 시그널 발행"""
        message = {
            "timestamp": datetime.now().isoformat(),
            **signal_data
        }
        
        try:
            message_id = self.redis_client.xadd("signals.tradable", message)
            logger.info(f"거래 시그널 발행: {message_id}")
            return message_id
        except Exception as e:
            logger.error(f"거래 시그널 발행 실패: {e}")
            return None
    
    def publish_order(self, order_data: Dict):
        """주문 발행"""
        message = {
            "timestamp": datetime.now().isoformat(),
            **order_data
        }
        
        try:
            message_id = self.redis_client.xadd("orders.submitted", message)
            logger.info(f"주문 발행: {message_id}")
            return message_id
        except Exception as e:
            logger.error(f"주문 발행 실패: {e}")
            return None
    
    def publish_fill(self, fill_data: Dict):
        """체결 발행"""
        message = {
            "timestamp": datetime.now().isoformat(),
            **fill_data
        }
        
        try:
            message_id = self.redis_client.xadd("orders.fills", message)
            logger.info(f"체결 발행: {message_id}")
            return message_id
        except Exception as e:
            logger.error(f"체결 발행 실패: {e}")
            return None
    
    def publish_risk_update(self, risk_data: Dict):
        """리스크 업데이트 발행"""
        message = {
            "timestamp": datetime.now().isoformat(),
            **risk_data
        }
        
        try:
            message_id = self.redis_client.xadd("risk.pnl", message)
            logger.debug(f"리스크 업데이트 발행: {message_id}")
            return message_id
        except Exception as e:
            logger.error(f"리스크 업데이트 발행 실패: {e}")
            return None
    
    def consume_stream(self, stream_key: str, count: int = 10, 
                      block_ms: int = 1000, last_id: str = "0") -> List[StreamMessage]:
        """스트림 소비"""
        try:
            # XREAD로 메시지 읽기
            result = self.redis_client.xread(
                {stream_key: last_id},
                count=count,
                block=block_ms
            )
            
            messages = []
            for stream, stream_messages in result:
                for message_id, data in stream_messages:
                    # timestamp 파싱
                    timestamp_str = data.get("timestamp", "")
                    timestamp = datetime.fromisoformat(timestamp_str) if timestamp_str else datetime.now()
                    
                    message = StreamMessage(
                        stream=stream,
                        message_id=message_id,
                        data=data,
                        timestamp=timestamp
                    )
                    messages.append(message)
            
            return messages
            
        except Exception as e:
            logger.error(f"스트림 소비 실패 ({stream_key}): {e}")
            return []
    
    def consume_quotes(self, ticker: str, mic: str = "XNAS", 
                      count: int = 10, block_ms: int = 1000) -> List[StreamMessage]:
        """시세 스트림 소비"""
        stream_key = f"quotes.{mic}.{ticker}"
        return self.consume_stream(stream_key, count, block_ms)
    
    def consume_news(self, count: int = 10, block_ms: int = 1000) -> List[StreamMessage]:
        """뉴스 스트림 소비"""
        return self.consume_stream("news.headlines", count, block_ms)
    
    def consume_edgar(self, count: int = 10, block_ms: int = 1000) -> List[StreamMessage]:
        """EDGAR 스트림 소비"""
        return self.consume_stream("news.edgar", count, block_ms)
    
    def consume_signals(self, count: int = 10, block_ms: int = 1000) -> List[StreamMessage]:
        """시그널 스트림 소비"""
        return self.consume_stream("signals.raw", count, block_ms)
    
    def consume_tradable_signals(self, count: int = 10, block_ms: int = 1000) -> List[StreamMessage]:
        """거래 가능한 시그널 스트림 소비"""
        return self.consume_stream("signals.tradable", count, block_ms)
    
    def consume_orders(self, count: int = 10, block_ms: int = 1000) -> List[StreamMessage]:
        """주문 스트림 소비"""
        return self.consume_stream("orders.submitted", count, block_ms)
    
    def consume_fills(self, count: int = 10, block_ms: int = 1000) -> List[StreamMessage]:
        """체결 스트림 소비"""
        return self.consume_stream("orders.fills", count, block_ms)
    
    def get_stream_length(self, stream_key: str) -> int:
        """스트림 길이 조회"""
        try:
            return self.redis_client.xlen(stream_key)
        except Exception as e:
            logger.error(f"스트림 길이 조회 실패 ({stream_key}): {e}")
            return 0
    
    def get_latest_message_id(self, stream_key: str) -> Optional[str]:
        """최신 메시지 ID 조회"""
        try:
            result = self.redis_client.xrevrange(stream_key, count=1)
            if result:
                return result[0][0]  # (message_id, data) 튜플의 첫 번째 요소
            return None
        except Exception as e:
            logger.error(f"최신 메시지 ID 조회 실패 ({stream_key}): {e}")
            return None
    
    def trim_stream(self, stream_key: str, max_len: int = 1000):
        """스트림 트림 (오래된 메시지 제거)"""
        try:
            self.redis_client.xtrim(stream_key, maxlen=max_len)
            logger.debug(f"스트림 트림 완료: {stream_key} (최대 {max_len}개)")
        except Exception as e:
            logger.error(f"스트림 트림 실패 ({stream_key}): {e}")
    
    def create_consumer_group(self, stream_key: str, group_name: str = None):
        """컨슈머 그룹 생성"""
        if group_name is None:
            group_name = self.consumer_group
        
        try:
            self.redis_client.xgroup_create(stream_key, group_name, id="0", mkstream=True)
            logger.info(f"컨슈머 그룹 생성: {stream_key} -> {group_name}")
        except redis.ResponseError as e:
            if "BUSYGROUP" in str(e):
                logger.debug(f"컨슈머 그룹 이미 존재: {group_name}")
            else:
                logger.error(f"컨슈머 그룹 생성 실패: {e}")
        except Exception as e:
            logger.error(f"컨슈머 그룹 생성 실패: {e}")
    
    def read_from_group(self, stream_key: str, group_name: str, consumer_name: str,
                       count: int = 10, block_ms: int = 1000) -> List[StreamMessage]:
        """컨슈머 그룹에서 메시지 읽기"""
        try:
            result = self.redis_client.xreadgroup(
                group_name,
                consumer_name,
                {stream_key: ">"},  # ">" = latest (요구사항)
                count=count,
                block=block_ms
            )
            
            messages = []
            for stream, stream_messages in result:
                for message_id, data in stream_messages:
                    timestamp_str = data.get("timestamp", "")
                    timestamp = datetime.fromisoformat(timestamp_str) if timestamp_str else datetime.now()
                    
                    message = StreamMessage(
                        stream=stream,
                        message_id=message_id,
                        data=data,
                        timestamp=timestamp
                    )
                    messages.append(message)
            
            return messages
            
        except Exception as e:
            logger.error(f"그룹 읽기 실패 ({stream_key}): {e}")
            return []
    
    def acknowledge_message(self, stream_key: str, group_name: str, message_id: str):
        """메시지 확인 (ACK)"""
        try:
            self.redis_client.xack(stream_key, group_name, message_id)
            logger.debug(f"메시지 ACK: {stream_key} -> {message_id}")
        except Exception as e:
            logger.error(f"메시지 ACK 실패: {e}")
    
    def get_pending_messages(self, stream_key: str, group_name: str) -> List[Dict]:
        """대기 중인 메시지 조회"""
        try:
            result = self.redis_client.xpending(stream_key, group_name)
            return result
        except Exception as e:
            logger.error(f"대기 메시지 조회 실패: {e}")
            return []
    
    def health_check(self) -> Dict[str, Any]:
        """헬스 체크"""
        try:
            # Redis 연결 확인
            self.redis_client.ping()
            
            # 스트림 상태 확인
            stream_status = {}
            for stream_name, stream_key in self.streams.items():
                if "{" in stream_key:  # 템플릿 키는 건너뛰기
                    continue
                length = self.get_stream_length(stream_key)
                stream_status[stream_name] = {
                    "key": stream_key,
                    "length": length,
                    "latest_id": self.get_latest_message_id(stream_key)
                }
            
            return {
                "status": "healthy",
                "redis_connected": True,
                "streams": stream_status,
                "timestamp": datetime.now().isoformat()
            }
            
        except Exception as e:
            logger.error(f"헬스 체크 실패: {e}")
            return {
                "status": "unhealthy",
                "redis_connected": False,
                "error": str(e),
                "timestamp": datetime.now().isoformat()
            }

class StreamConsumer:
    """Redis Streams 소비자 (bot 그룹)"""
    
    def __init__(self, redis_streams: RedisStreams, consumer_name: str = "worker_1"):
        """
        Args:
            redis_streams: Redis Streams 인스턴스
            consumer_name: 컨슈머 이름
        """
        self.redis_streams = redis_streams
        self.consumer_name = consumer_name
        self.group_name = "bot"  # 요구사항에 맞게 고정
        
        # 소비할 스트림들
        self.streams = [
            "news.edgar",
            "signals.tradable"
        ]
        
        # 컨슈머 그룹 초기화
        self._init_consumer_groups()
        
        logger.info(f"Stream Consumer 초기화: {consumer_name}")
    
    def _init_consumer_groups(self):
        """컨슈머 그룹 초기화"""
        for stream_key in self.streams:
            try:
                self.redis_streams.create_consumer_group(stream_key, self.group_name)
            except Exception as e:
                logger.error(f"컨슈머 그룹 초기화 실패 ({stream_key}): {e}")
    
    def consume_edgar_events(self, count: int = 10, block_ms: int = 1000) -> List[StreamMessage]:
        """EDGAR 이벤트 소비"""
        return self.redis_streams.read_from_group(
            "news.edgar",
            self.group_name,
            self.consumer_name,
            count=count,
            block_ms=block_ms
        )
    
    def consume_tradable_signals(self, count: int = 10, block_ms: int = 1000) -> List[StreamMessage]:
        """거래 가능한 시그널 소비"""
        return self.redis_streams.read_from_group(
            "signals.tradable",
            self.group_name,
            self.consumer_name,
            count=count,
            block_ms=block_ms
        )
    
    def consume_quotes(self, ticker: str, mic: str = "XNAS", 
                      count: int = 10, block_ms: int = 1000) -> List[StreamMessage]:
        """시세 데이터 소비 (동적 스트림)"""
        stream_key = f"quotes.{mic}.{ticker}"
        
        # 동적 스트림의 경우 컨슈머 그룹 생성 시도
        try:
            self.redis_streams.create_consumer_group(stream_key, self.group_name)
        except:
            pass  # 이미 존재하거나 실패해도 계속 진행
        
        return self.redis_streams.read_from_group(
            stream_key,
            self.group_name,
            self.consumer_name,
            count=count,
            block_ms=block_ms
        )
    
    def acknowledge(self, stream_key: str, message_id: str):
        """메시지 확인"""
        self.redis_streams.acknowledge_message(stream_key, self.group_name, message_id)
    
    def get_pending_count(self, stream_key: str) -> int:
        """대기 중인 메시지 수 조회"""
        pending = self.redis_streams.get_pending_messages(stream_key, self.group_name)
        # redis-py 버전에 따라 dict 또는 tuple/list 반환 가능
        try:
            if isinstance(pending, dict):
                return int(pending.get("pending", 0))
            if isinstance(pending, (list, tuple)) and len(pending) >= 4:
                return int(pending[3])
        except Exception:
            pass
        return 0
