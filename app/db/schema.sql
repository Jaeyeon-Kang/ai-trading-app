-- PostgreSQL 데이터베이스 스키마
-- 거래 기록, 시그널, 리스크 지표 등

-- EDGAR 공시 이벤트 테이블 (최소 요구사항)
CREATE TABLE IF NOT EXISTS edgar_events (
    id SERIAL PRIMARY KEY,
    ts TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    ticker VARCHAR(10) NOT NULL,
    form VARCHAR(10) NOT NULL, -- '8-K', '4', etc.
    item TEXT, -- 8-K 아이템들
    url TEXT,
    snippet_hash VARCHAR(64) UNIQUE, -- 중복 방지용 해시
    snippet_text TEXT
);

-- 30초봉 OHLCV 데이터 테이블 (최소 요구사항)
CREATE TABLE IF NOT EXISTS bars_30s (
    id SERIAL PRIMARY KEY,
    ts TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    ticker VARCHAR(10) NOT NULL,
    o DECIMAL(10,4) NOT NULL, -- open
    h DECIMAL(10,4) NOT NULL, -- high
    l DECIMAL(10,4) NOT NULL, -- low
    c DECIMAL(10,4) NOT NULL, -- close
    v INTEGER NOT NULL, -- volume
    spread_est DECIMAL(10,4) DEFAULT 0 -- 스프레드 추정치
);

-- 시그널 테이블 (최소 요구사항에 맞춰 수정)
CREATE TABLE IF NOT EXISTS signals (
    id SERIAL PRIMARY KEY,
    ts TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    ticker VARCHAR(10) NOT NULL,
    regime VARCHAR(20) NOT NULL, -- 'trend', 'vol_spike', 'mean_revert', 'sideways'
    tech DECIMAL(5,4) NOT NULL, -- 기술적 점수 0~1
    sentiment DECIMAL(5,4) NOT NULL, -- 감성 점수 0~1
    score DECIMAL(5,4) NOT NULL, -- 최종 점수 0~1
    reason TEXT, -- 시그널 발생 이유
    horizon_min INTEGER, -- 예상 지속 시간 (분)
    override BOOLEAN DEFAULT FALSE -- EDGAR 오버라이드 여부
);

-- 페이퍼 주문 테이블 (최소 요구사항)
CREATE TABLE IF NOT EXISTS orders_paper (
    id SERIAL PRIMARY KEY,
    ts TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    ticker VARCHAR(10) NOT NULL,
    side VARCHAR(10) NOT NULL, -- 'buy', 'sell'
    qty INTEGER NOT NULL,
    px_entry DECIMAL(10,4) NOT NULL, -- 진입가
    sl DECIMAL(10,4), -- stop loss
    tp DECIMAL(10,4), -- take profit
    slippage_est DECIMAL(5,4) DEFAULT 0 -- 슬리피지 추정치
);

-- 페이퍼 체결 테이블 (최소 요구사항)
CREATE TABLE IF NOT EXISTS fills_paper (
    id SERIAL PRIMARY KEY,
    order_id INTEGER REFERENCES orders_paper(id),
    ts TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    px_fill DECIMAL(10,4) NOT NULL -- 체결가
);

-- 일일 메트릭 테이블 (최소 요구사항)
CREATE TABLE IF NOT EXISTS metrics_daily (
    id SERIAL PRIMARY KEY,
    date DATE UNIQUE NOT NULL,
    trades INTEGER DEFAULT 0,
    winrate DECIMAL(5,4) DEFAULT 0,
    rr_avg DECIMAL(5,4) DEFAULT 0, -- 평균 손익비
    pnl DECIMAL(10,4) DEFAULT 0,
    drawdown DECIMAL(5,4) DEFAULT 0,
    var95 DECIMAL(5,4) DEFAULT 0, -- VaR 95%
    latency_p99 DECIMAL(10,4) DEFAULT 0, -- 99퍼센타일 지연시간
    llm_cost_krw DECIMAL(10,2) DEFAULT 0 -- LLM 비용 (원)
);

-- 기존 테이블들 (호환성 유지)
CREATE TABLE IF NOT EXISTS signals_legacy (
    id SERIAL PRIMARY KEY,
    signal_id VARCHAR(50) UNIQUE NOT NULL,
    ticker VARCHAR(10) NOT NULL,
    signal_type VARCHAR(10) NOT NULL, -- 'buy', 'sell', 'hold'
    score DECIMAL(5,4) NOT NULL, -- -1.0000 ~ +1.0000
    confidence DECIMAL(5,4) NOT NULL, -- 0.0000 ~ 1.0000
    regime VARCHAR(20) NOT NULL, -- 'trend', 'vol_spike', 'mean_revert', 'sideways'
    tech_score DECIMAL(5,4) NOT NULL,
    sentiment_score DECIMAL(5,4) NOT NULL,
    edgar_override BOOLEAN DEFAULT FALSE,
    trigger TEXT,
    summary TEXT,
    entry_price DECIMAL(10,4),
    stop_loss DECIMAL(10,4),
    take_profit DECIMAL(10,4),
    horizon_minutes INTEGER,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    processed_at TIMESTAMP WITH TIME ZONE,
    status VARCHAR(20) DEFAULT 'pending' -- 'pending', 'approved', 'rejected', 'executed'
);

-- 거래 테이블
CREATE TABLE IF NOT EXISTS trades (
    id SERIAL PRIMARY KEY,
    trade_id VARCHAR(50) UNIQUE NOT NULL,
    signal_id VARCHAR(50) REFERENCES signals_legacy(signal_id),
    ticker VARCHAR(10) NOT NULL,
    side VARCHAR(10) NOT NULL, -- 'buy', 'sell'
    quantity INTEGER NOT NULL,
    price DECIMAL(10,4) NOT NULL,
    order_type VARCHAR(20) DEFAULT 'market', -- 'market', 'limit'
    limit_price DECIMAL(10,4),
    stop_loss DECIMAL(10,4),
    take_profit DECIMAL(10,4),
    slippage DECIMAL(5,4), -- 실제 슬리피지
    commission DECIMAL(10,4) DEFAULT 0,
    realized_pnl DECIMAL(10,4), -- 실현손익
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    filled_at TIMESTAMP WITH TIME ZONE,
    closed_at TIMESTAMP WITH TIME ZONE,
    status VARCHAR(20) DEFAULT 'pending' -- 'pending', 'filled', 'cancelled', 'rejected'
);

-- 포지션 테이블
CREATE TABLE IF NOT EXISTS positions (
    id SERIAL PRIMARY KEY,
    ticker VARCHAR(10) NOT NULL,
    quantity INTEGER NOT NULL,
    avg_price DECIMAL(10,4) NOT NULL,
    unrealized_pnl DECIMAL(10,4) DEFAULT 0,
    last_price DECIMAL(10,4),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(ticker)
);

-- 일일 통계 테이블
CREATE TABLE IF NOT EXISTS daily_stats (
    id SERIAL PRIMARY KEY,
    date DATE UNIQUE NOT NULL,
    trades_count INTEGER DEFAULT 0,
    win_count INTEGER DEFAULT 0,
    loss_count INTEGER DEFAULT 0,
    win_rate DECIMAL(5,4) DEFAULT 0,
    realized_pnl DECIMAL(10,4) DEFAULT 0,
    unrealized_pnl DECIMAL(10,4) DEFAULT 0,
    total_pnl DECIMAL(10,4) DEFAULT 0,
    avg_rr DECIMAL(5,4) DEFAULT 0, -- 평균 손익비
    max_drawdown DECIMAL(5,4) DEFAULT 0,
    var_95 DECIMAL(5,4) DEFAULT 0,
    position_count INTEGER DEFAULT 0,
    total_exposure DECIMAL(10,4) DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 리스크 이벤트 테이블
CREATE TABLE IF NOT EXISTS risk_events (
    id SERIAL PRIMARY KEY,
    event_type VARCHAR(20) NOT NULL, -- 'warning', 'critical', 'shutdown'
    daily_pnl_pct DECIMAL(5,4),
    var_95 DECIMAL(5,4),
    position_count INTEGER,
    total_exposure DECIMAL(10,4),
    reason TEXT,
    actions_taken TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- EDGAR 공시 테이블 (기존)
CREATE TABLE IF NOT EXISTS edgar_filings (
    id SERIAL PRIMARY KEY,
    ticker VARCHAR(10) NOT NULL,
    form_type VARCHAR(10) NOT NULL, -- '8-K', '4', etc.
    filing_date DATE NOT NULL,
    items TEXT[], -- 8-K 아이템들
    summary TEXT,
    impact_score DECIMAL(5,4) DEFAULT 0,
    url TEXT,
    cik VARCHAR(20),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(ticker, form_type, filing_date)
);

-- LLM 사용량 테이블
CREATE TABLE IF NOT EXISTS llm_usage (
    id SERIAL PRIMARY KEY,
    date DATE NOT NULL,
    calls_count INTEGER DEFAULT 0,
    cost_usd DECIMAL(10,4) DEFAULT 0,
    status VARCHAR(20) DEFAULT 'active', -- 'active', 'limited', 'disabled'
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(date)
);

-- 시스템 로그 테이블
CREATE TABLE IF NOT EXISTS system_logs (
    id SERIAL PRIMARY KEY,
    level VARCHAR(10) NOT NULL, -- 'INFO', 'WARNING', 'ERROR'
    component VARCHAR(50) NOT NULL, -- 'scheduler', 'risk_engine', 'llm_engine'
    message TEXT NOT NULL,
    details JSONB,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 성능 지표 테이블
CREATE TABLE IF NOT EXISTS performance_metrics (
    id SERIAL PRIMARY KEY,
    metric_name VARCHAR(50) NOT NULL, -- 'signal_latency', 'execution_latency'
    value DECIMAL(10,4) NOT NULL,
    unit VARCHAR(10), -- 'ms', 'seconds', 'percent'
    timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 인덱스 생성 (최소 요구사항 테이블들)
CREATE INDEX IF NOT EXISTS idx_edgar_events_ticker ON edgar_events(ticker);
CREATE INDEX IF NOT EXISTS idx_edgar_events_ts ON edgar_events(ts);
CREATE INDEX IF NOT EXISTS idx_edgar_events_form ON edgar_events(form);
CREATE INDEX IF NOT EXISTS idx_bars_30s_ticker ON bars_30s(ticker);
CREATE INDEX IF NOT EXISTS idx_bars_30s_ts ON bars_30s(ts);
CREATE INDEX IF NOT EXISTS idx_signals_ticker ON signals(ticker);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);
CREATE INDEX IF NOT EXISTS idx_signals_regime ON signals(regime);
CREATE INDEX IF NOT EXISTS idx_orders_paper_ticker ON orders_paper(ticker);
CREATE INDEX IF NOT EXISTS idx_orders_paper_ts ON orders_paper(ts);
CREATE INDEX IF NOT EXISTS idx_fills_paper_order_id ON fills_paper(order_id);
CREATE INDEX IF NOT EXISTS idx_metrics_daily_date ON metrics_daily(date);

-- 기존 인덱스들 (호환성 유지)
CREATE INDEX IF NOT EXISTS idx_signals_legacy_ticker ON signals_legacy(ticker);
CREATE INDEX IF NOT EXISTS idx_signals_legacy_created_at ON signals_legacy(created_at);
CREATE INDEX IF NOT EXISTS idx_signals_legacy_status ON signals_legacy(status);
CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades(ticker);
CREATE INDEX IF NOT EXISTS idx_trades_created_at ON trades(created_at);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_daily_stats_date ON daily_stats(date);
CREATE INDEX IF NOT EXISTS idx_risk_events_created_at ON risk_events(created_at);
CREATE INDEX IF NOT EXISTS idx_edgar_filings_ticker ON edgar_filings(ticker);
CREATE INDEX IF NOT EXISTS idx_edgar_filings_date ON edgar_filings(filing_date);
CREATE INDEX IF NOT EXISTS idx_system_logs_level ON system_logs(level);
CREATE INDEX IF NOT EXISTS idx_system_logs_created_at ON system_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_performance_metrics_name ON performance_metrics(metric_name);
CREATE INDEX IF NOT EXISTS idx_performance_metrics_timestamp ON performance_metrics(timestamp);

-- 뷰 생성 (최소 요구사항 기반)
CREATE OR REPLACE VIEW signal_summary_minimal AS
SELECT 
    DATE(ts) as date,
    COUNT(*) as total_signals,
    AVG(score) as avg_score,
    COUNT(CASE WHEN override = true THEN 1 END) as override_signals,
    COUNT(CASE WHEN regime = 'trend' THEN 1 END) as trend_signals,
    COUNT(CASE WHEN regime = 'vol_spike' THEN 1 END) as vol_spike_signals,
    COUNT(CASE WHEN regime = 'mean_revert' THEN 1 END) as mean_revert_signals
FROM signals
GROUP BY DATE(ts)
ORDER BY date DESC;

CREATE OR REPLACE VIEW trade_summary_minimal AS
SELECT 
    DATE(o.ts) as date,
    COUNT(*) as total_orders,
    COUNT(f.id) as total_fills,
    AVG(f.px_fill - o.px_entry) as avg_slippage,
    SUM(CASE WHEN o.side = 'buy' THEN 1 ELSE 0 END) as buy_orders,
    SUM(CASE WHEN o.side = 'sell' THEN 1 ELSE 0 END) as sell_orders
FROM orders_paper o
LEFT JOIN fills_paper f ON o.id = f.order_id
GROUP BY DATE(o.ts)
ORDER BY date DESC;

-- 기존 뷰들 (호환성 유지)
CREATE OR REPLACE VIEW signal_summary AS
SELECT 
    DATE(created_at) as date,
    COUNT(*) as total_signals,
    COUNT(CASE WHEN signal_type = 'buy' THEN 1 END) as buy_signals,
    COUNT(CASE WHEN signal_type = 'sell' THEN 1 END) as sell_signals,
    AVG(score) as avg_score,
    AVG(confidence) as avg_confidence,
    COUNT(CASE WHEN edgar_override = true THEN 1 END) as edgar_signals
FROM signals_legacy
GROUP BY DATE(created_at)
ORDER BY date DESC;

CREATE OR REPLACE VIEW trade_summary AS
SELECT 
    DATE(created_at) as date,
    COUNT(*) as total_trades,
    COUNT(CASE WHEN side = 'buy' THEN 1 END) as buy_trades,
    COUNT(CASE WHEN side = 'sell' THEN 1 END) as sell_trades,
    SUM(realized_pnl) as total_pnl,
    AVG(ABS(realized_pnl)) as avg_pnl,
    COUNT(CASE WHEN realized_pnl > 0 THEN 1 END) as win_trades,
    COUNT(CASE WHEN realized_pnl < 0 THEN 1 END) as loss_trades
FROM trades
WHERE status = 'filled'
GROUP BY DATE(created_at)
ORDER BY date DESC;

-- 함수 생성
CREATE OR REPLACE FUNCTION update_daily_stats()
RETURNS TRIGGER AS $$
BEGIN
    -- 일일 통계 업데이트
    INSERT INTO daily_stats (date, trades_count, realized_pnl)
    VALUES (DATE(NEW.created_at), 1, COALESCE(NEW.realized_pnl, 0))
    ON CONFLICT (date) DO UPDATE SET
        trades_count = daily_stats.trades_count + 1,
        realized_pnl = daily_stats.realized_pnl + COALESCE(NEW.realized_pnl, 0),
        updated_at = CURRENT_TIMESTAMP;
    
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- 트리거 생성
DROP TRIGGER IF EXISTS trigger_update_daily_stats ON trades;
CREATE TRIGGER trigger_update_daily_stats
    AFTER INSERT ON trades
    FOR EACH ROW
    EXECUTE FUNCTION update_daily_stats();

-- 초기 데이터 삽입 (선택사항)
INSERT INTO metrics_daily (date, trades, winrate, rr_avg, pnl, drawdown, var95, latency_p99, llm_cost_krw)
VALUES (CURRENT_DATE, 0, 0, 0, 0, 0, 0, 0, 0)
ON CONFLICT (date) DO NOTHING;

-- 권한 설정 (필요시)
-- GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO trading_bot_user;
-- GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO trading_bot_user;
