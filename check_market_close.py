#!/usr/bin/env python3

from app.adapters.alpaca_paper_trading import get_alpaca_client
from datetime import datetime, timedelta

try:
    client = get_alpaca_client()
    positions = client.get_positions()
    account = client.get_account_info()
    
    print('=== 📊 알파카 계정 현황 (장 마감 후) ===')
    print(f'현금: ${account["cash"]:,.2f}')
    print(f'포트폴리오 가치: ${account["portfolio_value"]:,.2f}')  
    print(f'총 자산: ${account["equity"]:,.2f}')
    print(f'매수력: ${account["buying_power"]:,.2f}')
    print()
    
    print('=== 🏠 현재 포지션 ===')
    if positions:
        total_unrealized = 0
        for pos in positions:
            pnl_color = '🟢' if pos.unrealized_pnl >= 0 else '🔴'
            print(f'{pnl_color} {pos.ticker}: {pos.quantity}주')
            print(f'   평균단가: ${pos.avg_price:.2f} → 현재가: ${pos.current_price:.2f}')
            print(f'   시가총액: ${pos.market_value:,.2f}')
            print(f'   미실현 P&L: ${pos.unrealized_pnl:,.2f} ({pos.unrealized_pnl_pct:.2f}%)')
            total_unrealized += pos.unrealized_pnl
            print()
        print(f'💰 총 미실현 손익: ${total_unrealized:,.2f}')
    else:
        print('포지션이 없습니다.')
        
    print('=== 🕐 시장 상태 확인 ===')
    market_open = client.is_market_open()
    print(f'시장 상태: {"🟢 열림" if market_open else "🔴 닫힘"}')
    
except Exception as e:
    print(f'오류 발생: {e}')