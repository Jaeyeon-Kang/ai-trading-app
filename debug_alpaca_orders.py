#!/usr/bin/env python3
"""
알파카 주문 히스토리 직접 확인 스크립트
"""

import os
from datetime import datetime, timedelta
from alpaca.trading.client import TradingClient

def check_alpaca_orders():
    """알파카 주문 히스토리 확인"""
    
    # 환경변수에서 API 키 로드
    api_key = os.getenv('ALPACA_API_KEY')
    api_secret = os.getenv('ALPACA_API_SECRET')
    
    if not api_key or not api_secret:
        print("❌ ALPACA_API_KEY와 ALPACA_API_SECRET 환경변수가 필요합니다")
        return
    
    # 알파카 클라이언트 초기화
    trading_client = TradingClient(
        api_key=api_key,
        secret_key=api_secret,
        paper=True
    )
    
    print("🔍 알파카 페이퍼 계정 주문 히스토리 조회...")
    
    try:
        # 최근 100개 주문 조회
        orders = trading_client.get_orders(limit=100)
        
        print(f"📊 총 {len(orders)}건의 주문 발견")
        print("=" * 80)
        
        aapl_orders = []
        for order in orders:
            if order.symbol == 'AAPL':
                aapl_orders.append(order)
                
                print(f"🍎 AAPL 주문 발견:")
                print(f"  - 주문 ID: {order.id}")
                print(f"  - 심볼: {order.symbol}")
                print(f"  - 방향: {order.side.value}")
                print(f"  - 수량: {order.qty}")
                print(f"  - 상태: {order.status}")
                print(f"  - 주문 시간: {order.created_at}")
                if order.filled_at:
                    print(f"  - 체결 시간: {order.filled_at}")
                    print(f"  - 체결가: ${order.filled_avg_price}")
                print("-" * 40)
        
        print(f"\n📈 AAPL 주문 총 {len(aapl_orders)}건")
        
        # 최근 24시간 주문만 필터링
        recent_orders = []
        cutoff_time = datetime.now() - timedelta(hours=24)
        
        for order in aapl_orders:
            if order.created_at and order.created_at.replace(tzinfo=None) > cutoff_time:
                recent_orders.append(order)
        
        print(f"⏰ 최근 24시간 AAPL 주문: {len(recent_orders)}건")
        
        if recent_orders:
            print("\n🚨 최근 AAPL 주문 상세:")
            for order in recent_orders:
                print(f"  {order.created_at} | {order.side.value} {order.qty}주 | {order.status}")
        
        # 계정 정보도 확인
        account = trading_client.get_account()
        print(f"\n💰 계정 잔고: ${float(account.cash):,.2f}")
        print(f"📊 포트폴리오 가치: ${float(account.portfolio_value):,.2f}")
        
        # 현재 포지션 확인
        positions = trading_client.get_all_positions()
        aapl_positions = [pos for pos in positions if pos.symbol == 'AAPL']
        
        if aapl_positions:
            print(f"\n🍎 AAPL 포지션:")
            for pos in aapl_positions:
                print(f"  수량: {pos.qty}주")
                print(f"  평균단가: ${pos.avg_entry_price}")
                print(f"  현재가: ${pos.current_price}")
                print(f"  손익: ${pos.unrealized_pl} ({float(pos.unrealized_plpc)*100:.2f}%)")
        else:
            print("\n✅ 현재 AAPL 포지션 없음")
            
    except Exception as e:
        print(f"❌ 알파카 API 호출 실패: {e}")

if __name__ == "__main__":
    # .env 파일 로드
    try:
        with open('.env', 'r') as f:
            for line in f:
                if '=' in line and not line.startswith('#'):
                    key, value = line.strip().split('=', 1)
                    os.environ[key] = value
    except FileNotFoundError:
        print("⚠️  .env 파일을 찾을 수 없습니다")
    
    check_alpaca_orders()