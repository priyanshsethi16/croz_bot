# Industrial Product Catalog Parser

Processes large industrial PDF catalogs (100–1000 pages), generates one **clean markdown chunk per product**, stores structured data in PostgreSQL, and indexes chunks in self-hosted Qdrant for dense + BM25 hybrid RAG.

---

## Architecture

```
src/
├── main.py                      # Orchestrator / CLI entry point
├── config.py                    # Config from .env + config.yaml
├── ocr/
│   └── mistral_ocr.py           # Mistral OCR API, page-by-page with retry
├── extraction/
│   ├── boundary_detector.py     # Rule-based + LLM product boundary detection
│   ├── product_parser.py        # LLM structured extraction (Groq/Qwen)
│   └── chunk_generator.py       # Markdown chunk rendering + file output
├── models/
│   └── product.py               # Pydantic schemas
└── utils/
    ├── logger.py                # Structured logging (console + file)
    └── file_utils.py            # OCR cache, chunk saving, checkpoints
```

### Data flow

```
PDF
 │
 ▼
[Mistral OCR]  ──cache──▶  data/ocr/page_001.md ... page_N.md
 │
 ▼
[Boundary Detector]
  1. Rule-based pass  (headings, product numbers, code changes)
  2. LLM refinement   (Groq / Qwen3-32b) for multi-page products
 │
 ▼
[Product Parser]  ──LLM──▶  structured Product (Pydantic)
 │
 ▼
[Chunk Generator]
  chunks/0001_club_hammer.md
  chunks/0002_sledge_hammer.md
  data/products.json
```

---

## Setup

```bash
# 1. Clone / unzip
cd catalog_parser

# 2. Create venv
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure API keys
cp .env.example .env
# Edit .env → add MISTRAL_API_KEY and GROQ_API_KEY
```

---

## Usage

```bash
# Basic run
python -m src.main --pdf path/to/catalog.pdf

# Use existing OCR cache (skip Mistral API calls)
python -m src.main --pdf catalog.pdf --skip-ocr

# Ignore checkpoint (re-run from scratch)
python -m src.main --pdf catalog.pdf --no-resume

# Rule-based boundary detection only (no LLM refinement)
python -m src.main --pdf catalog.pdf --rules-only

# Custom config file
python -m src.main --pdf catalog.pdf --config my_config.yaml
```

---

## Output

### Chunks

```
chunks/
├── 0001_club_hammers.md
├── 0002_sledge_hammer.md
├── 0003_sledge_hammer_din_6475.md
└── ...
```

### Chunk format

```markdown
# Club Hammers

**Product Code:** `CHID`
**Category:** Hammers

## Description
Heavy-duty club hammer for general construction...

## Features
- Drop-forged steel head
- Lacquered shaft

## Utility
- Breaking masonry
- Driving stakes

## Specifications
- **Standard:** DIN 1042
- **Finish:** Lacquered

## Ordering Information
| Cat No | Ord No | Weight | Dimensions |
| ------ | ------ | ------ | ---------- |
| CH500  | 102345 | 500g   | 280mm      |

## Source Pages
11–13

## Raw Product Text
<details>
<summary>Show raw OCR text</summary>
...
</details>
```

### JSON manifest

`data/products.json` — array of all products with full schema:

```json
[
  {
    "product_id": "PROD_0001",
    "product_name": "Club Hammers",
    "product_code": "CHID",
    "page_start": 11,
    "page_end": 13,
    "category": "Hammers",
    "description": "...",
    "features": [...],
    "utilities": [...],
    "specifications": {...},
    "variants": [...],
    "ordering_information": [...],
    "chunk_path": "chunks/0001_club_hammers.md"
  }
]
```

---

## Resume / Checkpointing

Processing automatically checkpoints after every page OCR'd and every product extracted.

If interrupted, re-run the same command — it will resume from where it stopped.

To force a fresh start: `--no-resume`

---

## Performance Notes

| Catalog size | Estimated time           |
|-------------|--------------------------|
| 50 pages    | ~5 min (OCR) + ~3 min (LLM) |
| 200 pages   | ~20 min + ~12 min        |
| 500 pages   | ~50 min + ~30 min        |

OCR is cached — re-runs skip already-processed pages entirely.

---

## Configuration

All settings in `config.yaml`. API keys in `.env` (never commit `.env`).

Key settings:

| Setting | Default | Description |
|---------|---------|-------------|
| `ocr.model` | `mistral-ocr-latest` | Mistral OCR model |
| `llm.model` | `qwen/qwen3-32b` | Groq model for extraction |
| `llm.temperature` | `0.1` | LLM temperature (keep low) |
| `processing.resume_from_checkpoint` | `true` | Auto-resume on restart |
