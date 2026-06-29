# Vision Pipeline

A layout-aware product catalog parser that extracts structured product data directly from PDF page images using vision-language models (Gemini, OpenAI GPT-4/5, or Groq) — no OCR step needed.

## How It Works

```
PDF → PNG pages → Vision Model → product JSON → markdown chunks
```

Each page image is sent directly to the vision model, which reads columns, tables, badges, and layout visually. This avoids the column-layout confusion that OCR-based pipelines suffer from.

## Project Structure

```
vision_pipeline/
├── main.py               # Entry point
├── gemini_extractor.py   # Gemini vision provider (LangChain)
├── openai_extractor.py   # OpenAI GPT-4/5 vision provider
├── vision_extractor.py   # Groq Llama vision provider
├── key_rotator.py        # Multi-provider key rotation
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
pip install pdf2image pyyaml python-dotenv pypdf tqdm pillow langchain-google-genai openai groq
sudo apt-get install -y poppler-utils   # Linux / WSL
```

**2. Set your API key(s)** in `.env` (project root):
```
# Choose one or more providers:
GEMINI_API_KEY=your_gemini_key
OPENAI_API_KEY=your_openai_key
GROQ_API_KEY=your_groq_key
```

- Gemini key: [aistudio.google.com](https://aistudio.google.com)
- OpenAI key: [platform.openai.com/api-keys](https://platform.openai.com/api-keys)
- Groq key: [console.groq.com](https://console.groq.com)

**3. Select provider in config.yaml**:
```yaml
provider: "openai"  # Options: "gemini", "openai", "groq"
```

## Usage

```bash
# Process full PDF with OpenAI GPT-4o
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

## Models

| Provider | Example models | Vision | Configuration |
|---|---|---|---|
| Gemini | `gemini-2.5-flash` | ✅ | config.yaml → gemini.gemini_model |
| OpenAI | `gpt-5.4`, `gpt-5.4-mini`, `gpt-5`, `gpt-4o`, `gpt-4o-mini`, `o1` | ✅ | config.yaml → openai.openai_model |
| Groq | `llama-4-scout-17b-16e-instruct` | ✅ | config.yaml → groq.vision_model |

See [OPENAI_MODELS.md](OPENAI_MODELS.md) for complete list of OpenAI GPT-4/5 series models.

## Rate Limits

| Provider | Free Tier | Paid Tier |
|---|---|---|
| Gemini | ~35-50 pages/day (500k tokens) | Higher limits with billing |
| OpenAI | No free tier | Generous limits, pay-per-use |
| Groq | ~15 RPM free | Higher with subscription |

The pipeline adds delays between calls to respect rate limits. Use multiple API keys for key rotation:
```
OPENAI_API_KEY=key1
OPENAI_API_KEY_1=key2
OPENAI_API_KEY_2=key3
```

## Known Limitations

- Dense pages with 6+ products may have some data dropped due to output token limits — increase `max_tokens` in `config.yaml` if needed
- Pages processed at 150 DPI by default — increase `dpi` in `config.yaml` for higher quality at the cost of larger image size and more tokens
