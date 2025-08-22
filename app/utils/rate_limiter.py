"""
API 토큰 버킷 레이트 리미터
Redis 기반 분산 토큰 관리로 분당 10콜 제한 구현
Tier A(6), Tier B(3), 예약(1) 차등 할당
"""
import json
import logging
import time
from typing import Dict, Tuple
from enum import Enum

import redis

from app.config import settings

logger = logging.getLogger(__name__)

class TokenTier(Enum):
    """토큰 Tier 타입"""
    TIER_A = "tier_a"
    TIER_B = "tier_b"
    RESERVE = "reserve"

class APIRateLimiter:
    """API 토큰 버킷 레이트 리미터"""
    
    def __init__(self, redis_url: str = None):
        """
        Args:
            redis_url: Redis 연결 URL
        """
        self.redis_url = redis_url or "redis://redis:6379/0"
        self.redis_client = redis.from_url(self.redis_url)
        
        # Tier별 토큰 할당량 (분당)
        self.tier_allocations = {
            TokenTier.TIER_A: settings.API_TIER_A_ALLOCATION,
            TokenTier.TIER_B: settings.API_TIER_B_ALLOCATION,
            TokenTier.RESERVE: settings.API_RESERVE_ALLOCATION
        }
        
        # Redis 키 prefix
        self.key_prefix = "api_tokens"
        
        logger.info(f"API 레이트 리미터 초기화: A={self.tier_allocations[TokenTier.TIER_A]}, "
                   f"B={self.tier_allocations[TokenTier.TIER_B]}, "
                   f"예약={self.tier_allocations[TokenTier.RESERVE]}")
    
    def _get_redis_key(self, tier: TokenTier) -> str:
        """Redis 키 생성"""
        return f"{self.key_prefix}:{tier.value}"
    
    def _get_current_minute_bucket(self) -> int:
        """현재 분 버킷 (분 단위 시간 창)"""
        return int(time.time() // 60)
    
    def _refill_tokens_if_needed(self, tier: TokenTier) -> None:
        """필요시 토큰 리필 (분 단위)"""
        key = self._get_redis_key(tier)
        current_minute = self._get_current_minute_bucket()
        
        # 현재 토큰 상태 조회
        bucket_data = self.redis_client.get(key)
        
        if bucket_data is None:
            # 새로운 버킷 생성
            bucket = {
                "tokens": self.tier_allocations[tier],
                "last_refill_minute": current_minute
            }
            self.redis_client.setex(key, 120, json.dumps(bucket))  # 2분 TTL
            logger.debug(f"새 토큰 버킷 생성: {tier.value} = {bucket['tokens']}개")
            return
        
        try:
            bucket = json.loads(bucket_data)
            last_refill_minute = bucket.get("last_refill_minute", 0)
            
            # 분이 바뀌었으면 토큰 리필
            if current_minute > last_refill_minute:
                bucket["tokens"] = self.tier_allocations[tier]
                bucket["last_refill_minute"] = current_minute
                self.redis_client.setex(key, 120, json.dumps(bucket))
                logger.info(f"🔄 토큰 리필: {tier.value}={bucket['tokens']}개 (minute: {current_minute:02d})")
                
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"토큰 버킷 데이터 오류: {e}, 리셋")
            bucket = {
                "tokens": self.tier_allocations[tier],
                "last_refill_minute": current_minute
            }
            self.redis_client.setex(key, 120, json.dumps(bucket))
    
    def can_consume_token(self, tier: TokenTier, count: int = 1) -> bool:
        """
        토큰 소비 가능 여부 확인 (실제 소비하지 않음)
        
        Args:
            tier: 토큰 Tier
            count: 요청할 토큰 수
            
        Returns:
            bool: 소비 가능 여부
        """
        try:
            self._refill_tokens_if_needed(tier)
            
            key = self._get_redis_key(tier)
            bucket_data = self.redis_client.get(key)
            
            if bucket_data is None:
                return False
            
            bucket = json.loads(bucket_data)
            current_tokens = bucket.get("tokens", 0)
            
            return current_tokens >= count
            
        except Exception as e:
            logger.error(f"토큰 확인 오류: {e}")
            return False
    
    def consume_token(self, tier: TokenTier, count: int = 1) -> bool:
        """
        토큰 소비 (원자적 연산)
        
        Args:
            tier: 토큰 Tier
            count: 소비할 토큰 수
            
        Returns:
            bool: 소비 성공 여부
        """
        try:
            self._refill_tokens_if_needed(tier)
            
            key = self._get_redis_key(tier)
            
            # Lua 스크립트로 원자적 토큰 소비
            lua_script = """
            local key = KEYS[1]
            local count = tonumber(ARGV[1])
            
            local bucket_data = redis.call('GET', key)
            if not bucket_data then
                return 0
            end
            
            local bucket = cjson.decode(bucket_data)
            local current_tokens = bucket.tokens or 0
            
            if current_tokens >= count then
                bucket.tokens = current_tokens - count
                redis.call('SETEX', key, 120, cjson.encode(bucket))
                return 1
            else
                return 0
            end
            """
            
            result = self.redis_client.eval(lua_script, 1, key, count)
            success = bool(result)
            
            if success:
                logger.debug(f"토큰 소비 성공: {tier.value} -{count}개")
            else:
                logger.debug(f"토큰 부족: {tier.value} (요청: {count}개)")
                
            return success
            
        except Exception as e:
            logger.error(f"토큰 소비 오류: {e}")
            return False
    
    def get_token_status(self) -> Dict[str, Dict]:
        """
        모든 Tier의 토큰 상태 조회
        
        Returns:
            Dict: Tier별 토큰 상태
        """
        status = {}
        
        for tier in TokenTier:
            try:
                self._refill_tokens_if_needed(tier)
                
                key = self._get_redis_key(tier)
                bucket_data = self.redis_client.get(key)
                
                if bucket_data:
                    bucket = json.loads(bucket_data)
                    status[tier.value] = {
                        "current_tokens": bucket.get("tokens", 0),
                        "max_tokens": self.tier_allocations[tier],
                        "last_refill_minute": bucket.get("last_refill_minute", 0)
                    }
                else:
                    status[tier.value] = {
                        "current_tokens": 0,
                        "max_tokens": self.tier_allocations[tier],
                        "last_refill_minute": 0
                    }
                    
            except Exception as e:
                logger.error(f"토큰 상태 조회 오류 ({tier.value}): {e}")
                status[tier.value] = {
                    "current_tokens": 0,
                    "max_tokens": self.tier_allocations[tier],
                    "last_refill_minute": 0,
                    "error": str(e)
                }
        
        return status
    
    def get_total_api_usage(self) -> Dict[str, int]:
        """
        전체 API 사용량 통계
        
        Returns:
            Dict: 사용량 통계
        """
        status = self.get_token_status()
        
        total_used = 0
        total_available = 0
        
        for tier_status in status.values():
            max_tokens = tier_status["max_tokens"]
            current_tokens = tier_status["current_tokens"]
            
            total_available += max_tokens
            total_used += max_tokens - current_tokens
        
        return {
            "total_used": total_used,
            "total_available": total_available,
            "total_capacity": settings.API_CALLS_PER_MINUTE,
            "usage_pct": (total_used / settings.API_CALLS_PER_MINUTE) * 100 if settings.API_CALLS_PER_MINUTE > 0 else 0
        }
    
    def reset_all_tokens(self) -> bool:
        """
        모든 토큰 버킷 리셋 (긴급용)
        
        Returns:
            bool: 리셋 성공 여부
        """
        try:
            for tier in TokenTier:
                key = self._get_redis_key(tier)
                self.redis_client.delete(key)
            
            logger.info("모든 토큰 버킷 리셋 완료")
            return True
            
        except Exception as e:
            logger.error(f"토큰 버킷 리셋 오류: {e}")
            return False
    
    def try_consume_with_fallback(self, primary_tier: TokenTier, fallback_tier: TokenTier = None) -> Tuple[bool, TokenTier]:
        """
        토큰 소비 시도, 실패시 fallback tier 사용
        
        Args:
            primary_tier: 우선 사용할 Tier
            fallback_tier: 실패시 사용할 Tier (None이면 reserve 사용)
            
        Returns:
            Tuple[bool, TokenTier]: (성공 여부, 실제 사용된 Tier)
        """
        # 1차 시도: Primary tier
        if self.consume_token(primary_tier):
            return True, primary_tier
        
        # 2차 시도: Fallback tier
        if fallback_tier is None:
            fallback_tier = TokenTier.RESERVE
            
        if self.consume_token(fallback_tier):
            logger.info(f"Fallback 토큰 사용: {primary_tier.value} -> {fallback_tier.value}")
            return True, fallback_tier
        
        # 모든 시도 실패
        logger.warning(f"토큰 소비 실패: {primary_tier.value}, {fallback_tier.value} 모두 부족")
        return False, primary_tier

# 글로벌 인스턴스
rate_limiter = APIRateLimiter()

def get_rate_limiter() -> APIRateLimiter:
    """글로벌 레이트 리미터 인스턴스 반환"""
    return rate_limiter