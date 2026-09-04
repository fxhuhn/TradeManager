-- migrations/004_account_metrics.sql
-- Erstellt die Tabelle account_metrics zur Persistierung des Kontostands und der Margin-Kennzahlen

CREATE TABLE IF NOT EXISTS account_metrics (
    account_id TEXT PRIMARY KEY,
    net_liquidation REAL NOT NULL,
    total_cash_value REAL NOT NULL,
    available_funds REAL NOT NULL,
    maint_margin_req REAL NOT NULL,
    cushion_pct REAL NOT NULL,
    buying_power REAL NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
