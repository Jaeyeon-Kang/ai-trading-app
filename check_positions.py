#!/usr/bin/env python3

from app.adapters.alpaca_paper_trading import get_alpaca_client

try:
    client = get_alpaca_client()
    positions = client.get_positions()
    account = client.get_account_info()
    
    print('=== 계정 정보 ===')
    print(f'현금: ${account["cash"]:,.2f}')
    print(f'포트폴리오 가치: ${account["portfolio_value"]:,.2f}')
    print(f'매수력: ${account["buying_power"]:,.2f}')
    print()
    
    print('=== 현재 포지션 ===')
    if positions:
        for pos in positions:
            print(f'{pos.ticker}: {pos.quantity}주 @ ${pos.avg_price:.2f} (현재가: ${pos.current_price:.2f})')
            print(f'  시가총액: ${pos.market_value:,.2f}')  
            print(f'  미실현 손익: ${pos.unrealized_pnl:,.2f} ({pos.unrealized_pnl_pct:.1f}%)')
            print()
    else:
        print('포지션이 없습니다.')
        
    print('=== 최근 거래 내역 ===')
    orders = client.get_order_history(limit=10)
    for order in orders:
        print(f'{order["filled_at"]}: {order["symbol"]} {order["side"]} {order["quantity"]}주 @ ${order["filled_price"]:.2f}')
        
except Exception as e:
    print(f'오류: {e}')