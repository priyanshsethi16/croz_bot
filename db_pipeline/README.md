# DB Pipeline

Ingests catalog `.md` chunks into PostgreSQL (structured) + self-hosted Qdrant (dense semantic + sparse BM25 vectors).

## Setup

**1. Install dependencies:**
```bash
pip install psycopg2-binary "qdrant-client[fastembed]" langchain-qdrant
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
QDRANT_URL=http://127.0.0.1:6333
QDRANT_COLLECTION=catalog_chunks_v1
OPENAI_API_KEY=your_rotated_openai_key
```

**4. Start self-hosted Qdrant:**
```bash
qdrant --config-path config/qdrant.yaml --disable-telemetry
```

On the configured development machine it runs as an enabled user service:
```bash
systemctl --user status qdrant.service
```

Dense embeddings use OpenAI `text-embedding-3-small`; sparse vectors use `Qdrant/bm25`.

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
    └─► Qdrant (catalog_chunks_v1 collection)
          dense vector + sparse BM25 vector + filterable metadata

Query flow:
  semantic_search("demolition hammer")
      → Qdrant runs dense and sparse prefetches
      → Qdrant fuses rankings with weighted RRF
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
