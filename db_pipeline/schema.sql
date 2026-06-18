-- PostgreSQL schema for catalog product storage
-- Run once: psql -U postgres -d catalog_db -f schema.sql

CREATE TABLE IF NOT EXISTS products (
    id              SERIAL PRIMARY KEY,
    product_code    TEXT,
    product_name    TEXT NOT NULL,
    category        TEXT,
    source_pdf      TEXT,
    page_num        INTEGER,
    chunk_file      TEXT,
    markdown_text   TEXT,                    -- full raw .md content
    metadata        JSONB DEFAULT '{}',      -- features, specs, variants, ordering tables
    ingested_at     TIMESTAMP DEFAULT NOW()
);

-- Indexes for common query patterns
CREATE INDEX IF NOT EXISTS idx_products_code     ON products (product_code);
CREATE INDEX IF NOT EXISTS idx_products_category ON products (category);
CREATE INDEX IF NOT EXISTS idx_products_pdf      ON products (source_pdf);
CREATE INDEX IF NOT EXISTS idx_products_metadata ON products USING gin (metadata);
