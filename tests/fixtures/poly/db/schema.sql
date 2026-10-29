CREATE TABLE orders (
  order_id TEXT PRIMARY KEY,
  amount NUMERIC NOT NULL CHECK (amount > 0),
  currency TEXT NOT NULL DEFAULT 'JPY'
);

CREATE TABLE receipts (
  receipt_id INTEGER PRIMARY KEY,
  order_id TEXT NOT NULL REFERENCES orders(order_id),
  issued_at TEXT NOT NULL
);
