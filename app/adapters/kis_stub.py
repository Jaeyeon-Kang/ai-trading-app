"""
KIS 증권 주문 인터페이스 스텁
실제 호출은 금지, 인터페이스만 정의
"""
from dataclasses import dataclass
from typing import Optional, Literal
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

@dataclass
class OrderRequest:
    """주문 요청 데이터"""
    ticker: str
    side: Literal["buy", "sell"]
    quantity: int
    order_type: Literal["market", "limit"] = "market"
    limit_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    meta: dict = None  # 시그널 메타데이터

@dataclass
class OrderResponse:
    """주문 응답 데이터"""
    order_id: str
    status: Literal["pending", "filled", "cancelled", "rejected"]
    filled_price: Optional[float] = None
    filled_quantity: Optional[int] = None
    timestamp: datetime = None
    error_message: Optional[str] = None

class KISOrderAdapter:
    """KIS 증권 주문 어댑터 스텁"""
    
    def __init__(self, api_key: str, api_secret: str, account_no: str):
        """
        Args:
            api_key: KIS API 키
            api_secret: KIS API 시크릿
            account_no: 계좌번호
        """
        self.api_key = api_key
        self.api_secret = api_secret
        self.account_no = account_no
        self.base_url = "https://openapi.koreainvestment.com:9443"
        
        logger.info(f"KIS 어댑터 초기화: 계좌 {account_no}")
    
    def place_order(self, order: OrderRequest) -> OrderResponse:
        """
        주문 실행 (스텁 - 실제 호출 금지)
        
        Args:
            order: 주문 요청
            
        Returns:
            OrderResponse: 주문 응답 (스텁)
        """
        logger.warning("KIS 주문 스텁 호출됨 - 실제 거래 금지")
        
        # 스텁 응답 생성
        return OrderResponse(
            order_id=f"STUB_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
            status="pending",
            timestamp=datetime.now()
        )
    
    def get_account_balance(self) -> dict:
        """계좌 잔고 조회 (스텁)"""
        logger.warning("KIS 잔고 조회 스텁 호출됨")
        return {
            "cash_krw": 1000000,  # 100만원
            "cash_usd": 0.0,
            "total_value": 1000000
        }
    
    def get_positions(self) -> list:
        """보유 포지션 조회 (스텁)"""
        logger.warning("KIS 포지션 조회 스텁 호출됨")
        return []
