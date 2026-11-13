CREATE TABLE IF NOT EXISTS items (
    item_id          TEXT PRIMARY KEY,
    access_token     TEXT NOT NULL,
    institution_name TEXT,
    products         TEXT,              -- comma list: transactions,investments
    env              TEXT,
    created_at       TEXT DEFAULT (datetime('now')),
    txn_cursor       TEXT,
    last_synced      TEXT,
    last_error       TEXT
);
CREATE TABLE IF NOT EXISTS accounts (
    account_id        TEXT PRIMARY KEY,
    item_id           TEXT,
    institution_name  TEXT,
    name              TEXT,
    official_name     TEXT,
    mask              TEXT,
    type              TEXT,             -- depository | credit | loan | investment | other
    subtype           TEXT,
    current_balance   REAL,
    available_balance REAL,
    credit_limit      REAL,
    currency          TEXT,
    updated_at        TEXT
);
CREATE TABLE IF NOT EXISTS balance_history (
    date            TEXT,
    account_id      TEXT,
    current_balance REAL,
    PRIMARY KEY (date, account_id)
);
-- amount > 0 means money LEFT the account (spending); amount < 0 means money came IN.
CREATE TABLE IF NOT EXISTS transactions (
    transaction_id    TEXT PRIMARY KEY,
    account_id        TEXT,
    date              TEXT,
    authorized_date   TEXT,
    name              TEXT,
    merchant_name     TEXT,
    amount            REAL,
    currency          TEXT,
    category          TEXT,             -- Plaid personal_finance_category.primary, e.g. FOOD_AND_DRINK
    category_detailed TEXT,
    payment_channel   TEXT,
    pending           INTEGER
);
CREATE INDEX IF NOT EXISTS idx_txn_date ON transactions(date);
CREATE TABLE IF NOT EXISTS securities (
    security_id       TEXT PRIMARY KEY,
    name              TEXT,
    ticker            TEXT,
    type              TEXT,             -- equity | etf | mutual fund | cryptocurrency | cash | ...
    close_price       REAL,
    close_price_as_of TEXT
);
CREATE TABLE IF NOT EXISTS holdings (
    account_id  TEXT,
    security_id TEXT,
    quantity    REAL,
    price       REAL,
    value       REAL,
    cost_basis  REAL,
    price_as_of TEXT,
    PRIMARY KEY (account_id, security_id)
);
CREATE TABLE IF NOT EXISTS investment_transactions (
    investment_transaction_id TEXT PRIMARY KEY,
    account_id  TEXT,
    security_id TEXT,
    date        TEXT,
    name        TEXT,
    type        TEXT,                   -- buy | sell | cash | fee | transfer | cancel
    subtype     TEXT,
    quantity    REAL,
    price       REAL,
    amount      REAL,                   -- > 0 cash out of account (e.g. buy), < 0 cash in (e.g. sell, dividend)
    fees        REAL
);

-- ---- Amazon orders (from Amazon's order/shipping emails in Gmail; product names imported separately) ----
CREATE TABLE IF NOT EXISTS amazon_orders (
    order_id          TEXT PRIMARY KEY,   -- e.g. 114-9970154-7175459
    order_date        TEXT,               -- local date of the earliest email seen for the order
    summary           TEXT,               -- Amazon's email heading, e.g. "1 item: Phone Accessories"
    order_total       REAL,               -- grand total from the order confirmation
    total_is_estimate INTEGER DEFAULT 0,  -- 1 for grocery orders whose final charge can change
    status            TEXT,               -- ordered | shipped | delivered
    updated_at        TEXT
);
-- One row per "Shipped" email that states a total: Amazon charges the card per shipment.
CREATE TABLE IF NOT EXISTS amazon_shipments (
    email_id TEXT PRIMARY KEY,
    order_id TEXT,
    date     TEXT,
    summary  TEXT,
    amount   REAL
);
CREATE TABLE IF NOT EXISTS amazon_items (
    order_id   TEXT,
    title      TEXT,
    quantity   REAL,
    unit_price REAL,
    asin       TEXT,
    source     TEXT,                      -- export | browser | manual
    PRIMARY KEY (order_id, title)
);
-- Gmail messages already processed, so each sync only reads new mail.
CREATE TABLE IF NOT EXISTS amazon_emails (
    email_id   TEXT PRIMARY KEY,
    kind       TEXT,
    order_ids  TEXT,
    parsed_at  TEXT
);
