-- Day4: performance metrics enrichment

-- trades table enrichments (idempotent)
ALTER TABLE trades
  ADD COLUMN IF NOT EXISTS mfe_bp       INTEGER,
  ADD COLUMN IF NOT EXISTS mae_bp       INTEGER,
  ADD COLUMN IF NOT EXISTS slippage_bp  INTEGER,
  ADD COLUMN IF NOT EXISTS hold_minutes INTEGER,
  ADD COLUMN IF NOT EXISTS realized_r   NUMERIC;

-- daily performance summary table
CREATE TABLE IF NOT EXISTS perf_daily (
  dt DATE PRIMARY KEY,
  trades INTEGER,
  winrate NUMERIC,
  profit_factor NUMERIC,
  mfe_bp_avg INTEGER,
  mae_bp_avg INTEGER,
  slippage_bp_avg INTEGER,
  dd_max_bp INTEGER,
  equity_end NUMERIC
);


