# DB Pipeline

Ingests catalog `.md` chunks into PostgreSQL (structured) + ChromaDB (semantic embeddings).

## Setup

**1. Install dependencies:**
```bash
pip install psycopg2-binary chromadb sentence-transformers
```

**2. Install and start PostgreSQL (WSL):**
```bash
sudo apt-get install postgresql
sudo service postgresql start
sudo -u postgres psql -c "CREATE DATABASE catalog_db;"
```

**3. Add to `.env`:**
```
POSTGRES_URL=postgresql://postgres:postgres@localhost:5432/catalog_db
```

## Usage

**Ingest one PDF's chunks:**
```bash
python -m db_pipeline.ingest --chunks vision_pipeline/data/Workshop_mini/chunks
```

**Ingest all PDFs at once:**
```bash
python -m db_pipeline.ingest --all
```

**Re-ingest (wipe and reload):**
```bash
python -m db_pipeline.ingest --chunks vision_pipeline/data/Workshop_mini/chunks --force
```

**Search by exact product code:**
```bash
python -m db_pipeline.search --exact BPID
```

**Semantic search:**
```bash
python -m db_pipeline.search --semantic "hammer for demolition work"
python -m db_pipeline.search --semantic "grease pump" --category "Grease Pumps"
python -m db_pipeline.search --semantic "drill bits" --pdf Workshop_mini --top 10
```

## Architecture

```
.md chunks
    │
    ├─► PostgreSQL (products table)
    │     product_code, product_name, category, source_pdf,
    │     page_num, chunk_file, markdown_text, metadata (JSONB)
    │
    └─► ChromaDB (catalog_products collection)
          embedding vector + metadata (links back to PostgreSQL id)

Query flow:
  semantic_search("demolition hammer")
      → ChromaDB finds top-K similar embeddings
      → PostgreSQL fetches full structured data for those IDs
      → Returns combined result
```

## Per-PDF folder structure

Each PDF gets its own isolated output directory:
```
vision_pipeline/data/
├── Workshop_mini/
│   ├── pages/        ← PNG pages
│   ├── chunks/       ← .md product files
│   ├── products.json
│   └── checkpoint.json
├── Grease_Catalog/
│   ├── pages/
│   ├── chunks/
│   └── ...
```
