-- migrations/002_allow_multiple_exits.sql
-- Ermöglicht die Speicherung mehrerer Exits (z. B. LMT und LOC) in derselben Trade-Gruppe

-- 1. Fremdschlüssel-Prüfungen temporär ausschalten
PRAGMA foreign_keys = OFF;

-- 2. Neue Tabelle mit der aktualisierten UNIQUE-Constraint anlegen
CREATE TABLE orders_new (
    order_id INTEGER PRIMARY KEY,
    perm_id INTEGER,
    parent_id INTEGER,
    trade_group_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    bracket_role TEXT NOT NULL CHECK (bracket_role IN ('ENTRY', 'SL', 'TP', 'EXIT')),
    symbol TEXT NOT NULL,
    sec_type TEXT NOT NULL CHECK (sec_type = 'STK'),
    exchange TEXT NOT NULL CHECK (exchange = 'SMART'),
    action TEXT NOT NULL CHECK (action IN ('BUY', 'SELL')),
    quantity INTEGER NOT NULL,
    order_type TEXT NOT NULL,
    target_price REAL,
    tif TEXT DEFAULT 'GTC',
    strategy_name TEXT,
    status TEXT NOT NULL CHECK (status IN ('Created', 'Submitted', 'PreSubmitted', 'Filled', 'Cancelled', 'Error')),
    retry_count INTEGER DEFAULT 0,
    transmitted_at TIMESTAMP,
    FOREIGN KEY (parent_id) REFERENCES orders_new (order_id) ON UPDATE CASCADE,
    -- Aktualisierte Constraint für mehrere Exits:
    UNIQUE (account_id, trade_group_id, bracket_role, order_type)
);

-- 3. Daten aus der alten Tabelle kopieren
INSERT INTO orders_new SELECT * FROM orders;

-- 4. Alte Tabelle löschen
DROP TABLE orders;

-- 5. Neue Tabelle umbenennen
ALTER TABLE orders_new RENAME TO orders;

-- 6. Indexe neu erstellen
CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_perm_id 
ON orders (perm_id) 
WHERE perm_id IS NOT NULL AND perm_id != 0;

CREATE INDEX IF NOT EXISTS idx_orders_trade_group ON orders (trade_group_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders (status);

-- 7. Fremdschlüssel-Prüfungen wieder aktivieren
PRAGMA foreign_keys = ON;
