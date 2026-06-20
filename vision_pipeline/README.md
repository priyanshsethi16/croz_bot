# Vision Pipeline

A layout-aware product catalog parser that extracts structured product data directly from PDF page images using an admin-selected Gemini vision-language model via LangChain — no OCR step needed.

## How It Works

```
PDF → PNG pages → Gemini VLM (LangChain) → product JSON → markdown chunks
```

Each page image is sent directly to the vision model, which reads columns, tables, badges, and layout visually. This avoids the column-layout confusion that OCR-based pipelines suffer from.

## Project Structure

```
vision_pipeline/
├── main.py               # Entry point
├── gemini_extractor.py   # Sends page PNG to Gemini through LangChain
├── chunk_writer.py       # Converts product JSON to markdown files
├── pdf_to_images.py      # Rasterizes PDF pages to PNG
├── config.yaml           # Model, DPI, rate limits, output paths
└── data/
    ├── pages/            # Rasterized PNG pages (auto-created)
    ├── chunks/           # Output markdown files (one per product)
    ├── products.json     # Full extraction manifest
    └── checkpoint.json   # Resume state
```

## Setup

**1. Install dependencies** (from project root):
```bash
pip install pdf2image pyyaml python-dotenv pypdf tqdm pillow langchain-google-genai
sudo apt-get install -y poppler-utils   # Linux / WSL
```

**2. Set your API key(s)** in `.env` (project root):
```
GEMINI_API_KEY=your_gemini_key
```

- Gemini key: [aistudio.google.com](https://aistudio.google.com) — no credit card

When using the Django UI, configure the encrypted key and VLM from **Admin → Models & Keys** instead of editing `.env`.

**3. Select the Gemini model** in **Admin → Models & Keys**. CLI runs may set `GEMINI_VISION_MODEL`.

## Usage

```bash
# Process full PDF
python -m vision_pipeline.main --pdf /path/to/catalog.pdf

# Process specific page range (useful for testing)
python -m vision_pipeline.main --pdf /path/to/catalog.pdf --pages 7 15

# Start fresh, ignore existing checkpoint
python -m vision_pipeline.main --pdf /path/to/catalog.pdf --no-resume

# Reuse existing PNG pages (skip re-rasterization)
python -m vision_pipeline.main --pdf /path/to/catalog.pdf --skip-raster
```

## Output

Each product family gets its own markdown file in `vision_pipeline/data/chunks/`:

```
0001_machinist_squares.md
0002_machinist_square_sets.md
0003_engineers_precision_squares.md
...
```

Each file contains:
- Product name, code, category, source page
- Description, features, utility, specifications
- **Variants section** — every child variant (size/model) as its own sub-section with its own sizes table and ordering information table

## Rate Limits (Free Tier)

| Limit | Value |
|---|---|
| Tokens per minute | 30,000 |
| Tokens per day | 500,000 |
| Approx. pages per day | ~35–50 pages |

The pipeline adds a 5-second delay between calls to stay under the per-minute limit. If the daily limit is hit, the checkpoint is saved — just re-run the next day and it resumes from where it stopped.

The pipeline can rotate `GEMINI_API_KEY_1`, `GEMINI_API_KEY_2`, etc. for CLI batch jobs. The admin UI uses the single encrypted Gemini key configured by the administrator.

## Models

| Provider | Example model | Vision | Selection |
|---|---|---|---|
| Gemini | `gemini-2.5-flash` | ✅ | Admin → Models & Keys |

## Known Limitations

- Dense pages with 6+ products may have some data dropped due to output token limits — increase `max_tokens` in `config.yaml` if needed (max 8192)
- Pages processed at 300 DPI by default — increase `dpi` in `config.yaml` for higher quality at the cost of larger image size and more tokens
- Free tier daily limit of 500k tokens is the main throughput constraint
