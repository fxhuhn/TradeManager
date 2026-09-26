-- migrations/005_cash_ledger.sql
-- Erstellt die Tabelle cash_ledger und die View v_trade_settlement_all_in
-- für das lückenlose Tracking von Nebenkosten, Dividenden, Quellensteuern und Zinsen.

CREATE TABLE IF NOT EXISTS cash_ledger (
    ledger_id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    trade_group_id TEXT NULLABLE,
    symbol TEXT NULLABLE,
    category TEXT NOT NULL CHECK (
        category IN (
            'COMMISSION',
            'EXCHANGE_FEE',
            'CLEARING_FEE',
            'REGULATORY_FEE',
            'BORROW_FEE',
            'DIVIDEND',
            'WITHHOLDING_TAX',
            'PAYMENT_IN_LIEU',
            'INTEREST_DEBIT',
            'INTEREST_CREDIT',
            'SYEP_INCOME',
            'MARKET_DATA',
            'OTHER_FEE'
        )
    ),
    description TEXT NOT NULL,
    amount TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    fx_rate_to_base TEXT NOT NULL DEFAULT '1.0',
    amount_in_base TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'SETTLED' CHECK (status IN ('PENDING', 'SETTLED', 'CANCELLED')),
    effective_date DATE NOT NULL,
    settled_date DATE NULLABLE,
    source TEXT NOT NULL CHECK (source IN ('REALTIME_CALLBACK', 'FLEX_QUERY')),
    external_reference_id TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (account_id, external_reference_id, category, status)
);

CREATE INDEX IF NOT EXISTS idx_ledger_group ON cash_ledger (account_id, trade_group_id);
CREATE INDEX IF NOT EXISTS idx_ledger_date ON cash_ledger (effective_date);
CREATE INDEX IF NOT EXISTS idx_ledger_status ON cash_ledger (status);

CREATE VIEW IF NOT EXISTS v_trade_settlement_all_in AS
SELECT 
    ts.account_id,
    ts.trade_group_id,
    ts.avg_entry_price,
    ts.avg_exit_price,
    ts.price_diff_slippage,
    ts.total_commissions AS trading_commissions,
    ts.net_pnl AS trading_net_pnl,
    COALESCE(SUM(CASE WHEN cl.category = 'REGULATORY_FEE' THEN CAST(cl.amount AS REAL) ELSE 0 END), 0.0) AS reg_fees,
    COALESCE(SUM(CASE WHEN cl.category = 'BORROW_FEE' THEN CAST(cl.amount AS REAL) ELSE 0 END), 0.0) AS borrow_fees,
    COALESCE(SUM(CASE WHEN cl.category IN ('DIVIDEND', 'WITHHOLDING_TAX', 'PAYMENT_IN_LIEU') THEN CAST(cl.amount AS REAL) ELSE 0 END), 0.0) AS net_dividends,
    COALESCE(SUM(CASE WHEN cl.category = 'SYEP_INCOME' THEN CAST(cl.amount AS REAL) ELSE 0 END), 0.0) AS syep_income,
    ROUND(CAST(ts.net_pnl AS REAL) + COALESCE(SUM(CAST(cl.amount AS REAL)), 0.0), 2) AS all_in_net_pnl,
    CASE WHEN COUNT(cl.ledger_id) > 0 THEN 1 ELSE 0 END AS has_adjustments,
    ts.settled_at
FROM trades_settlement ts
LEFT JOIN cash_ledger cl 
    ON ts.account_id = cl.account_id 
   AND ts.trade_group_id = cl.trade_group_id
GROUP BY ts.account_id, ts.trade_group_id;
