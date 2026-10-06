CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

CREATE TABLE orders (
    id         serial PRIMARY KEY,
    customer   text NOT NULL,
    note       text,
    status     text NOT NULL DEFAULT 'new',
    created_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO orders (customer, note)
SELECT 'customer_' || (g % 5000), md5(g::text) FROM generate_series(1, 1000000) g;
ANALYZE orders;

-- The read-only role stackdoctor connects as (see README "Recommended: a read-only role").
CREATE ROLE stackdoctor_ro LOGIN PASSWORD 'readonly';
GRANT CONNECT ON DATABASE shop TO stackdoctor_ro;
GRANT USAGE ON SCHEMA public TO stackdoctor_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO stackdoctor_ro;
GRANT pg_monitor TO stackdoctor_ro;
ALTER ROLE stackdoctor_ro SET default_transaction_read_only = on;
