# Catalog-aware RAG: Implementation Plan

> Status: V2-only query architecture is active. The legacy query path and collection have been retired.

## Implementation status — 2026-06-20

The V2 code path described in this document is implemented and active for all queries.

Implemented and verified:

- Phase 0 backups, baseline, flags, and read-only audit command;
- Django V2 catalog/document/job/category/product/variant/chunk models and migrations;
- validated VLM schema, original-page offsets, and adjacent-page family assembly;
- checksum-safe upload, durable database-backed jobs, retry/cancel/archive, and worker command;
- review-gated structured persistence with deterministic UUIDs;
- isolated `catalog_chunks_v2` dense + BM25 collection and source-aware payload indexes;
- exact-count embedding confirmation, reconciliation, and activation commands;
- English deterministic query routing plus `gpt-4o-mini` decomposition fallback;
- PostgreSQL exact/list/count/comparison routes;
- source-filtered V2 hybrid retrieval, multi-query merge/deduplication, evidence completeness, and citations;
- manual-specific text chunking and product-code links;
- legacy dry-run/backfill, evaluation dataset, and admin review actions; and
- one V2-only query route with explicit errors instead of a hidden legacy fallback.

Current activation state:

- `catalog_chunks_v2` is the only live Qdrant collection and contains 1,040 points.
- 1,040 legacy families are represented by 52 active, ready V2 documents.
- Existing dense and BM25 vectors were migrated by exact content hash with zero OpenAI embedding calls.
- All migrated families are approved and all 1,040 chunks are indexed.
- Querying always uses V2. `CATALOG_RAG_V2_INGEST` remains disabled until the admin split/review workflow is fully wired to V2.

Future indexing remains review- and cost-gated. There is no legacy bulk-index button or query fallback.

### New-document checklist

Prefer re-uploading original catalogs through V2 so the validated VLM schema produces real variants. The review-gated legacy backfill is useful for migration/audit but contains zero structured variants and must not be bulk-approved blindly.

1. Set `CATALOG_RAG_V2_INGEST=true` in `.env` and restart Django.
2. Start `catalog_v2_worker` (a service template exists at `config/catalog-v2-worker.service`).
3. Upload/reprocess a catalog through the V2 admin upload path.
4. Review uncertain categories/products in `/django-admin/catalog/productfamily/`.
5. Run `catalog_v2_index <document-id>` to see the exact paid embedding count.
6. Execute indexing only with `--execute --confirm-embeddings <exact-count>` or the matching admin UI confirmation.
7. Run `catalog_v2_reconcile <document-id>` and the dry-run query evaluation.
8. Execute the six-query evaluation only with `catalog_v2_evaluate --execute --confirm-queries 6`.
9. Run `catalog_v2_activate` with the exact ready-document confirmation if the collection alias must be repaired.

No Pinecone dependency or hosted reranking service is used.

## Confirmed decisions

- End-user queries and assistant answers will be English only.
- Admins can upload multiple GROZ catalogs and, later, product manuals.
- Gemini VLM remains responsible for catalog extraction.
- Dense embeddings use OpenAI `text-embedding-3-small`.
- Qdrant remains the dense + BM25 sparse hybrid vector store.
- PostgreSQL is the source of truth for exhaustive product data.
- OpenAI `gpt-4o-mini` is the chat and query-planning model.
- Query decomposition is used only for complex multi-intent queries, not every query.
- A separate Qdrant collection per PDF will not be created. One versioned collection with payload filters will be used.

## Historical repository baseline

The implementation must evolve the current system without deleting or corrupting existing catalog data.

- Django upload currently writes PDFs directly under `input/` and identifies duplicates only by filename.
- `vision_pipeline.main` rasterizes pages, calls the VLM page by page, and writes `products.json` plus one Markdown file per extracted family.
- The VLM currently uses the section-banner text as `category`; this is not a normalized taxonomy.
- The retired bulk ingester previously read filesystem artifacts or the legacy PostgreSQL `products` table.
- The legacy `products` table has product/chunk rows but no catalog, document-version, variant, job, or normalized-category relationships.
- At planning time the legacy table contains 1,040 rows from 52 `source_pdf` values and 594 distinct raw category strings. This confirms that raw VLM categories cannot be used directly for exhaustive listing.
- Qdrant currently stores OpenAI dense vectors and FastEmbed/Qdrant BM25 sparse vectors, fused through weighted RRF.
- Qdrant point IDs are based mainly on product code/name plus content hash, without a stable catalog-document ID. Identical content in two catalogs can therefore lose source separation.
- Chat currently sends one query to `HybridRetriever(top_k=5)` and then to the answer model.
- PDF processing and indexing currently run through blocking `subprocess.run` calls with request-level timeouts.
- Current PDF deletion removes files and attempts Qdrant deletion, but does not provide transactional PostgreSQL cleanup or version rollback.

These constraints justified the V2 migration. The legacy collection and runtime path have now been removed.

## Goal

Every catalog PDF uploaded by an admin should be processed automatically into:

1. structured, exhaustive product data in PostgreSQL; and
2. searchable dense and sparse vectors in Qdrant.

This separation lets the assistant answer both semantic questions and complete-list questions reliably.

## Why this is needed

Vector similarity search returns the most relevant `top_k` chunks. It cannot guarantee an exhaustive answer to questions such as:

- "What hammers are available?"
- "Show me all types of pliers."
- "List every variant in this catalog."

These questions must use structured PostgreSQL data. Qdrant remains responsible for semantic retrieval, specifications, descriptions, and manual content.

## Target ingestion flow

```text
Admin uploads a PDF
        |
        v
Create Catalog/Document record in PostgreSQL
        |
        v
Parse pages with the VLM
        |
        v
Extract product-wise text and structured fields
        |
        v
Normalize categories and product families
        |
        +--> Save products and variants in PostgreSQL
        |
        +--> Create dense and sparse vectors
                  |
                  v
              Save in Qdrant with metadata
```

## Required VLM output

The VLM should not return only free-text chunks. It should return source-grounded product fields. Trusted IDs, filenames, and page offsets are injected by the pipeline rather than generated by the model. A validated extraction envelope should look similar to:

```json
{
  "schema_version": 2,
  "catalog_id": "7dbb0c88-e70b-46cf-984e-f942ae9b4614",
  "document_id": "526f8e2e-e682-4af8-b3fb-ef437eb51f14",
  "source_pdf": "Hand_Tool_Catalogue.pdf",
  "page_start": 114,
  "page_end": 114,
  "product_family": "Copper Hammers",
  "product_name": "Copper Head Sledge Hammer",
  "raw_category": "Copper Hammers",
  "normalized_category_suggestion": "hammer",
  "normalized_subcategory_suggestion": "sledge_hammer",
  "product_code": "CHID/4/12/CU",
  "specifications": {
    "weight_lb": 4,
    "handle_length_in": 12
  },
  "variants": [
    {
      "product_code": "CHID/4/12/CU",
      "order_number": "34600"
    }
  ],
  "extraction_confidence": 0.97
}
```

Minimum fields:

- `catalog_id`
- `document_id`
- `source_pdf`
- `page_start` and `page_end`
- `product_family`
- `product_name`
- raw category and normalized-category suggestion
- `product_code`
- variants and ordering information
- `specifications`
- extraction confidence/status

Markdown/vector chunks are generated by application code only after validation and cross-page family assembly.

## Storage responsibilities

### PostgreSQL

PostgreSQL should be the source of truth for:

- catalogs and document versions;
- complete product inventory;
- normalized categories and subcategories;
- product families and variants;
- product/order codes;
- structured specifications; and
- active/inactive document versions.

It should answer exhaustive and deterministic queries.

### Qdrant

Qdrant should store dense and sparse vectors for:

- semantic product questions;
- keyword/product-code searches;
- product descriptions;
- catalog passages; and
- manuals and troubleshooting content.

Use one collection with metadata filters rather than creating a separate collection for every PDF, unless strict tenant isolation is required.

Example Qdrant payload:

```json
{
  "catalog_id": "7dbb0c88-e70b-46cf-984e-f942ae9b4614",
  "document_id": "526f8e2e-e682-4af8-b3fb-ef437eb51f14",
  "product_family_id": "0b3da417-bc04-5f10-8248-9b77ee0eb4e8",
  "normalized_category": "hammer",
  "product_code": "CHID/4/12/CU",
  "source_type": "catalog",
  "source_pdf": "Hand_Tool_Catalogue.pdf",
  "page_start": 114,
  "page_end": 114,
  "is_active": true
}
```

## Category normalization

The extracted category must be normalized so that related product names can be listed together.

```text
Copper Hammer      -> category: hammer
Claw Hammer        -> category: hammer
Ball Pein Hammer   -> category: hammer
Sledge Hammer      -> category: hammer
Rubber Mallet      -> category: hammer, subcategory: mallet
```

Store both the original VLM label and the normalized label. Low-confidence or unknown categories should enter an admin review queue rather than being silently guessed.

## Query routing

```text
User query
    |
    v
Query intent router
    |
    +-- Specific specification question
    |       -> Qdrant hybrid retrieval
    |
    +-- Complex multi-product question
    |       -> Query decomposition
    |       -> Hybrid retrieval per subquery
    |       -> Merge, deduplicate, and rerank
    |
    +-- All/list/available/every question
    |       -> PostgreSQL exhaustive category query
    |       -> Group families and variants
    |
    +-- Manual/troubleshooting question
            -> Qdrant filtered by source_type=manual
```

### Example: complete listing

For `What hammers are available?`:

1. Detect the exhaustive-list intent.
2. Resolve the requested scope: current catalog, named catalog, or all active catalogs.
3. Query PostgreSQL using normalized `category = hammer`.
4. Group results by product family.
5. Deduplicate variants and product codes.
6. Paginate large result sets.
7. Let the chat model format only the retrieved structured results.

Do not use a fixed Qdrant `top_k` result as the complete inventory.

## Catalog scope and source filtering

Every request should have an explicit or resolved scope:

- current catalog: filter by `catalog_id`;
- specific document version: filter by `document_id`;
- all catalogs: search all active catalog versions;
- manuals only: filter by `source_type = manual`.

The UI should display the source PDF, page number, product code, and catalog/version for traceability.

## Duplicate uploads and versioning

The ingestion process must be idempotent:

- calculate a file checksum before processing;
- do not create duplicate products or vectors for the same PDF;
- mark older catalog versions inactive when an updated catalog is accepted;
- use document-scoped deterministic family, variant, and chunk IDs;
- when deleting a document, delete or deactivate its PostgreSQL records and Qdrant points; and
- retry failed pages without reprocessing successful pages.

## Target PostgreSQL schema

Create new Django-managed V2 tables. Do not rename or destructively alter the existing `products` table during rollout.

### `Catalog`

Represents a logical catalog across versions.

- UUID primary key
- name and slug
- brand
- optional description
- active document/version reference
- created and updated timestamps

### `CatalogDocument`

Represents one uploaded PDF or manual version.

- UUID primary key
- catalog foreign key
- `source_type`: `catalog` or `manual`
- original filename and managed file path
- SHA-256 checksum with a uniqueness constraint
- version number
- page count
- extraction schema version
- status: `uploaded`, `extracting`, `review`, `indexing`, `ready`, `failed`, `archived`
- `is_active`
- uploader and timestamps
- failure summary

Only one document version per catalog should be active at a time.

### `IngestionJob` and `ExtractionPage`

Tracks background work and makes page-level retries safe.

- document foreign key
- current stage and progress counts
- retry count
- started/completed timestamps
- sanitized error details
- per-page status, attempts, artifact path, and extraction result

### `Category`

Stores the controlled English taxonomy.

- canonical name and unique slug
- optional parent category
- English synonyms/aliases
- active flag

Raw VLM category text must be retained separately. A VLM suggestion must not silently create a new canonical category.

### `ProductFamily`

- UUID primary key
- document foreign key
- stable source key within the document
- product name and family code
- raw category text
- normalized category foreign key, nullable while under review
- description, features, utilities, materials, and specifications
- page start/end
- extraction confidence and review status
- raw extraction JSON for audit/debugging

### `ProductVariant`

- family foreign key
- product/catalog/order codes
- name
- size and unit fields where reliably available
- specifications and ordering data as JSON
- page start/end
- source-row hash for idempotency

### `DocumentChunk`

- UUID primary key
- document foreign key
- optional family and variant foreign keys
- `source_type` and chunk type
- Markdown/plain text
- page start/end
- SHA-256 content hash
- Qdrant point UUID
- index status: `pending`, `indexed`, `failed`, `stale`
- embedding model, dimensions, and extraction schema version

Important constraints:

- unique checksum per uploaded document;
- unique family source key per document;
- unique variant source-row hash per family;
- unique chunk identity per document and chunk hash/ordinal; and
- indexed foreign keys for document, category, codes, status, and active version.

## Stable identity strategy

Do not use filename, product name, or product code alone as the primary identity.

```text
document UUID = created once when the checksum is accepted
family UUID   = UUID5(document UUID, stable family source key)
variant UUID  = UUID5(family UUID, normalized code/order/row hash)
chunk UUID    = UUID5(document UUID, chunk type + family/variant UUID + content hash)
```

This allows the same product to exist in multiple catalogs without losing its source, while making retries for one document idempotent.

## VLM extraction contract

Refactor the extraction schema into a Pydantic model shared by the VLM pipeline and ingestion service.

Required changes:

- add `schema_version`, original document UUID, original page number, and extraction confidence;
- preserve `raw_category` from the page banner;
- add a separate `normalized_category_suggestion`;
- preserve family/child structure instead of reconstructing variants from Markdown;
- validate codes, page bounds, list/dict shapes, and required child rows;
- distinguish an empty product page from a failed extraction;
- retain raw VLM JSON for audit and repair;
- retry invalid JSON/schema output with validation feedback; and
- send low-confidence or unmapped records to review.

The current PDF splitter resets page numbering inside split PDFs. V2 must retain the original document UUID and page offset so citations always point to the original PDF page.

### Family assembly

Because VLM extraction is page-based, add an assembly stage before database insertion:

1. order page results by original page number;
2. join a product family continued across adjacent pages;
3. deduplicate repeated headers and ordering rows;
4. preserve every contributing page number;
5. flag uncertain joins for review; and
6. generate chunks only after family assembly.

This stage should prevent important continuation tables from becoming `Unknown Product` chunks.

## Category normalization strategy

Start with a controlled English taxonomy and an alias table. Example:

```text
hammer
  aliases: hammers, striking tool
  children: club hammer, claw hammer, ball pein hammer,
            sledge hammer, copper hammer, mallet
```

Normalization order:

1. exact canonical slug/name match;
2. exact alias match;
3. approved mapping from raw catalog section;
4. conservative model suggestion;
5. admin review when confidence is below the configured threshold.

Use normalized values for filtering/listing and raw values for display/audit. Add PostgreSQL `pg_trgm` indexes later for English spelling tolerance on names and categories; keep product/order-code matching normalized and exact.

## Qdrant V2 design

Create a new collection such as `catalog_chunks_v2`; do not reset the current collection. After validation, point a stable alias such as `catalog_chunks_current` to V2.

Keep the existing vector configuration:

- dense: OpenAI `text-embedding-3-small`, 1,536 dimensions, cosine distance;
- sparse: `Qdrant/bm25` with IDF; and
- retrieval: dense + sparse weighted RRF.

Required payload:

```json
{
  "catalog_id": "uuid",
  "document_id": "uuid",
  "document_version": 2,
  "product_family_id": "uuid",
  "product_variant_id": null,
  "product_name": "Copper Head Sledge Hammer",
  "product_code": "CHID/4/12/CU",
  "normalized_category": "hammer",
  "source_type": "catalog",
  "source_pdf": "Hand_Tool_Catalogue.pdf",
  "page_start": 114,
  "page_end": 114,
  "language": "en",
  "is_active": true,
  "schema_version": 2,
  "chunk_hash": "sha256"
}
```

Create payload indexes for document/catalog IDs, product codes, normalized category, source type, active state, page fields, and schema version.

### PostgreSQL/Qdrant consistency

PostgreSQL and Qdrant cannot share one transaction. Use a staged/outbox-style process:

1. commit validated products and chunks in PostgreSQL as `pending`;
2. upsert deterministic points to Qdrant;
3. mark each chunk `indexed`;
4. verify document point count and point IDs;
5. mark the document `ready`; and
6. only then activate the new version and deactivate the old one.

A reconciliation command must find missing, stale, duplicated, or orphaned Qdrant points. Never expose a half-indexed document as active.

## English-only query planner

Add a query-planning layer above the current `HybridRetriever`. The planner returns validated JSON; it never writes SQL directly.

Suggested plan shape:

```json
{
  "intent": "exhaustive_list",
  "scope": {"catalog_ids": [], "source_types": ["catalog"]},
  "entities": [{"type": "category", "value": "hammer"}],
  "constraints": {},
  "subqueries": [],
  "needs_clarification": false
}
```

Supported intents:

| Intent | Example | Route |
|---|---|---|
| `exact_lookup` | `Show CHID/4/12/CU` | PostgreSQL exact code lookup, then optional Qdrant enrichment |
| `exhaustive_list` | `What hammers are available?` | PostgreSQL normalized taxonomy query |
| `aggregation` | `How many hammer variants are available?` | PostgreSQL count/group query |
| `comparison` | `Compare IPW-304 and IPW-305` | PostgreSQL entity fetch plus relevant chunks |
| `recommendation` | `Recommend a non-sparking mining hammer` | Structured filters plus hybrid retrieval |
| `troubleshooting` | `Why will this pump not prime?` | Manual-filtered hybrid retrieval |
| `multi_intent` | `Build a four-item tool kit...` | Decompose, retrieve per subquery, merge/rerank |
| `general_semantic` | `Which tool is suitable for demolition?` | Direct hybrid retrieval |
| `ambiguous` | `Show me the best one` | Use conversation state or ask a clarification |

### Fast routing before the model

Use deterministic checks first for obvious cases:

- recognized product/order-code pattern;
- exhaustive words such as `all`, `every`, `available`, `list`, and `types`;
- aggregation words such as `count`, `how many`, and `total`;
- explicit catalog/manual scope from the UI.

Call `gpt-4o-mini` as the planner only when rules cannot safely determine the route or decomposition is needed. Limit decomposition to five focused subqueries.

### Query scope

- If the UI has a selected catalog, use that active document by default.
- Without a selected catalog, search all active catalog documents.
- Never search archived or partially indexed versions.
- For manuals, apply `source_type = manual` unless the planner explicitly needs both manuals and catalogs.
- When a request for `all` would produce a large response, return the complete count and paginated families rather than silently truncating it.

### Safe data access

Create explicit parameterized repository/service methods; do not let the model generate or execute arbitrary SQL.

Examples:

- `find_by_product_codes(codes, scope)`
- `list_category(category_slug, scope, page, page_size)`
- `count_category(category_slug, scope)`
- `compare_products(product_ids)`
- `hybrid_search(query, scope, filters, top_k)`
- `manual_search(query, product_ids, top_k)`

## Multi-intent retrieval and reranking

For complex questions:

1. produce two to five independent English subqueries;
2. run hybrid retrieval with the same catalog/source scope for each;
3. retain the subquery-to-evidence mapping;
4. deduplicate by chunk UUID, family UUID, and product code;
5. merge ranks with RRF;
6. boost exact product-code and structured-constraint matches;
7. enforce an evidence quota per requested entity; and
8. pass only the final grounded evidence package to the answer model.

Use deterministic reranking first. Add a paid model reranker only if evaluation shows a material recall/precision improvement.

## Grounded response contract

The answer layer should receive an evidence object containing:

- query plan and resolved scope;
- structured PostgreSQL rows;
- Qdrant chunks;
- source PDF and original page numbers;
- requested entities and whether each has evidence;
- total result count for exhaustive queries; and
- a `complete_result` flag set by code, never by the LLM.

The answer prompt must:

- answer in English;
- use only supplied evidence;
- never claim `all` unless `complete_result` is true;
- identify each missing requested item;
- preserve exact codes, units, and numeric values; and
- attach citations containing product, PDF, and page.

## Repository-specific implementation phases

Each phase has an exit gate. Do not proceed to production cutover merely because code compiles.

### Phase 0 — Baseline, backup, and feature flags

Target files: test suite, settings, management commands, this plan.

- Back up PostgreSQL and export the current Qdrant collection configuration/count.
- Record current retrieval answers for the five existing difficulty levels.
- Add an ingestion gate while the new review workflow is validated.
- Add a dry-run command that reports documents, products, duplicates, unmapped categories, and embeddings required.
- Confirm no command uses global `--reset` in the V2 path.

Exit gate: backups restore successfully and baseline evaluation is reproducible.

### Phase 1 — Django V2 data model

Target files: `groz_ui/catalog/models.py`, new migrations, admin registration, model tests.

- Add the V2 models and constraints described above.
- Keep the legacy `products` table untouched.
- Seed a small reviewed English taxonomy for the currently known top-level tool families.
- Add model/admin views for document status, failed pages, and category review.

Exit gate: migrations apply and reverse cleanly on a database copy; existing 1,040 rows remain unchanged.

### Phase 2 — Validated extraction and family assembly

Target files: `vision_pipeline/vision_extractor.py`, `gemini_extractor.py`, `main.py`, `chunk_writer.py`, and new schema/assembler modules.

- Introduce the shared Pydantic extraction schema.
- Preserve original page offsets for split PDFs.
- Validate and repair per-page outputs.
- Assemble continued families before chunk generation.
- Normalize categories conservatively and create review records for unresolved mappings.
- Store structured results through Django services rather than reparsing Markdown.

Exit gate: fixture PDFs produce deterministic families/variants with traceable original pages; invalid pages are retryable.

### Phase 3 — Idempotent background ingestion

Target files: catalog services/tasks, upload views, dashboard APIs/UI, management commands.

- Upload through `CatalogDocument`, streaming SHA-256 while saving.
- Reject an identical checksum before VLM calls.
- Move long-running extraction/indexing out of the HTTP request.
- Start with a database-backed worker/management command if infrastructure must remain minimal; use Celery + Redis when concurrent production workers are required.
- Use job/document locks so two workers cannot process the same document.
- Add retry, cancel, archive, and progress endpoints.

Exit gate: duplicate upload, worker restart, page retry, and cancellation do not duplicate products.

### Phase 4 — Qdrant V2 and reconciliation

Target files: `rag_pipeline/providers.py`, indexing/reconciliation services, and Qdrant tests.

- Create the versioned collection and payload indexes.
- Generate deterministic source-aware point IDs.
- Index only validated/approved chunks.
- Remove per-document points by document UUID, never by ambiguous filename/stem.
- Add reconciliation and per-document reindex commands.
- Verify point counts before marking a document ready.

Exit gate: reindex/delete affects only the selected document and leaves no orphaned points.

### Phase 5 — Query planner and retrieval services

Target files: new `rag_pipeline/planner.py`, repository/service modules, `retriever.py`, and unit/integration tests.

- Implement deterministic routing and validated `gpt-4o-mini` planning fallback.
- Add exact, exhaustive, aggregate, comparison, recommendation, manual, and multi-intent routes.
- Add catalog/document/source filters to every Qdrant call.
- Implement decomposition, merge, deduplication, evidence quotas, and deterministic reranking.
- Keep simple semantic questions on the current one-query fast path.

Exit gate: every planner intent selects the expected backend and scope in tests.

### Phase 6 — Chat orchestration and citations

Target files: `rag_pipeline/llm.py`, `chatbot.py`, Django chat views, chat template/JavaScript.

- Replace direct `retrieve -> answer` calls with `plan -> retrieve -> validate -> answer`.
- Return structured citations with catalog, PDF, page, product code, and score.
- Add pagination/follow-up controls for exhaustive lists.
- Preserve conversation references for questions such as `Show the heavier version`.
- Render Markdown headings/tables consistently.

Exit gate: the UI never labels a partial top-k result as a complete list and every factual answer exposes its source.

### Phase 7 — Backfill and cutover evaluation

- Backfill the 1,040 legacy rows into V2 in dry-run mode first.
- Map raw categories to the reviewed taxonomy and queue uncertain mappings.
- During migration, reuse matching dense/sparse vectors with new payload/IDs.
- Call OpenAI embeddings only for new or changed chunks; show the exact count and require admin confirmation before paid embedding work.
- Validate V2 queries before switching the stable Qdrant alias.

Exit gate: V2 meets the acceptance metrics below and its PostgreSQL/Qdrant backups restore successfully.

### Phase 8 — Manual ingestion

- Add a manual-specific parser that chunks by section/procedure rather than product family.
- Link manual chunks to recognized product codes/families where possible.
- Store `source_type = manual` and section/page metadata.
- Add troubleshooting and compatibility evaluation cases.

Exit gate: catalog listing queries exclude manuals while troubleshooting queries can cite them.

## Testing and evaluation plan

### Unit tests

- VLM schema validation and invalid-output handling
- category normalization and alias matching
- deterministic IDs and checksum deduplication
- family assembly across pages/splits
- query-intent routing and planner JSON validation
- SQL repository parameterization and pagination
- Qdrant payload filters, merge, deduplication, and reranking
- evidence completeness and answer fallback rules

### Integration tests

- PostgreSQL migrations and constraints
- one small fixture PDF through upload, extraction, validation, and indexing
- retry after a simulated VLM/page failure
- retry after a simulated Qdrant failure
- document archive/replacement with old version rollback
- isolated deletion from a test Qdrant collection

### Retrieval evaluation set

Maintain reviewed English questions in version control:

- exact product-code lookups;
- low/medium/high/advanced semantic questions;
- comparisons;
- multi-product kit requests;
- exhaustive lists such as `What hammers are available?`;
- counts and aggregations;
- typo/synonym cases;
- ambiguous follow-ups;
- missing-information cases; and
- manual troubleshooting cases when manuals are enabled.

Measure:

- exhaustive-list recall: must be 100% within the selected active scope;
- entity coverage for multi-intent questions: must include evidence for every requested entity or explicitly mark it missing;
- citation correctness;
- exact code/number/unit preservation;
- retrieval recall@k for semantic cases;
- unsupported-claim rate;
- P50/P95 latency; and
- VLM, embedding, and chat-model usage per document/query.

## Rollback and failure policy

- Activate new document versions only after PostgreSQL and Qdrant verification.
- Archive rather than immediately hard-delete active documents.
- On query failure, log and return a correlation ID; never hide the failure behind another retrieval path.
- Never log API keys, raw authorization headers, or decrypted secrets.
- A failed reindex must leave the previously active version searchable.

## Final acceptance criteria

- Uploading the same PDF twice causes no second VLM or embedding run.
- Every product, variant, and vector is traceable to document UUID, catalog/version, original PDF, and page.
- Reprocessing or deleting one catalog cannot affect another catalog.
- `What hammers are available?` returns every active hammer family and variant in the resolved scope, with a total count and pagination when required.
- Exact code lookup remains deterministic even if semantic retrieval would rank another product higher.
- Simple questions use one fast hybrid-search path.
- Complex multi-intent questions retrieve evidence separately for every requested entity.
- The assistant never presents top-k Qdrant results as an exhaustive inventory.
- Unknown/low-confidence extraction and taxonomy mappings are visible for admin review.
- No new OpenAI embeddings are generated during migration without a dry-run count and explicit admin action.
- V2 PostgreSQL and Qdrant backups can be restored without recreating embeddings.
