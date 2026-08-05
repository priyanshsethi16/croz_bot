"""Plan, route, retrieve, validate, and answer English V2 catalog queries."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from qdrant_client import models

from catalog.model_config import RuntimeModelConfig
from catalog.services.query_repository import (
    find_by_product_codes,
    list_products,
    list_products_filtered,
    product_to_chunk,
    resolve_category_term,
    resolve_product_code_tokens,
    source_chunks_for_products,
)
from rag_pipeline.ai_router import AIQueryRouter, AIQueryRouterError
from rag_pipeline.llm import LLMAnswerer
from rag_pipeline.planner import (
    QueryIntent,
    QueryPlan,
    QueryRoute,
    QueryScope,
    QueryTask,
    deterministic_plan,
    extract_requested_constraints,
)
from rag_pipeline.retriever import HybridRetriever


logger = logging.getLogger(__name__)


@dataclass
class QueryExecution:
    plan: QueryPlan
    standalone_query: str
    answer: str
    chunks: list[dict]
    sources: list[dict]
    complete_result: bool
    total_results: int | None
    page: int
    page_size: int
    missing_entities: list[str]

    def as_dict(self) -> dict:
        return {
            'answer': self.answer,
            'sources': self.sources,
            'query_plan': self.plan.model_dump(mode='json'),
            'complete_result': self.complete_result,
            'total_results': self.total_results,
            'page': self.page,
            'page_size': self.page_size,
            'missing_entities': self.missing_entities,
        }


def _scope_filter(scope: QueryScope) -> models.Filter:
    conditions = [
        models.FieldCondition(
            key='metadata.is_active',
            match=models.MatchValue(value=True),
        )
    ]
    if scope.source_types:
        conditions.append(models.FieldCondition(
            key='metadata.source_type',
            match=models.MatchAny(any=scope.source_types),
        ))
    if scope.catalog_ids:
        conditions.append(models.FieldCondition(
            key='metadata.catalog_id',
            match=models.MatchAny(any=scope.catalog_ids),
        ))
    if scope.document_ids:
        conditions.append(models.FieldCondition(
            key='metadata.document_id',
            match=models.MatchAny(any=scope.document_ids),
        ))
    return models.Filter(must=conditions)


def _category_entity(plan: QueryPlan) -> str:
    return next((entity.value for entity in plan.entities if entity.type == 'category'), '')


def _code_entities(plan: QueryPlan) -> list[str]:
    return [entity.value for entity in plan.entities if entity.type == 'product_code']


def _planned_product_codes(plan: QueryPlan) -> list[str]:
    codes: list[str] = []
    for code in _code_entities(plan):
        normalized = str(code or '').strip().upper()
        if normalized and normalized not in codes:
            codes.append(normalized)
    for task in plan.tasks:
        for code in task.product_codes:
            normalized = str(code or '').strip().upper()
            if normalized and normalized not in codes:
                codes.append(normalized)
    return codes


def _inventory_task(plan: QueryPlan) -> QueryTask | None:
    return next((task for task in plan.tasks if task.route == QueryRoute.POSTGRES_INVENTORY), None)


def _task_label(task: QueryTask) -> str:
    if task.route == QueryRoute.POSTGRES_EXACT and task.product_codes:
        return ', '.join(task.product_codes)
    return task.purpose or task.query


def _result_identity(result: dict) -> str:
    metadata = result.get('metadata', {})
    return str(
        metadata.get('chunk_hash')
        or metadata.get('product_family_id')
        or metadata.get('document_id', '') + ':' + str(metadata.get('page_start', ''))
        or result.get('text', '')[:120]
    )


def _merge_subquery_results(result_sets: list[tuple[str, list[dict]]], *, limit: int = 15):
    merged: dict[str, dict] = {}
    coverage: dict[str, bool] = {}
    for subquery, results in result_sets:
        coverage[subquery] = bool(results)
        for rank, result in enumerate(results, 1):
            key = _result_identity(result)
            contribution = 1.0 / (2 + rank)
            if key not in merged:
                merged[key] = {**result, '_fusion_score': 0.0, '_subqueries': []}
            merged[key]['_fusion_score'] += contribution
            merged[key]['_subqueries'].append(subquery)
    ranked = sorted(merged.values(), key=lambda item: item['_fusion_score'], reverse=True)[:limit]
    for item in ranked:
        item['score'] = round(item.pop('_fusion_score'), 4)
        item['metadata'] = {**item.get('metadata', {}), 'matched_subqueries': item.pop('_subqueries')}
    return ranked, [subquery for subquery, matched in coverage.items() if not matched]


def _markdown_cell(value) -> str:
    text = str(value or '').replace('\r', ' ').replace('\n', ' ').strip()
    return text.replace('|', '\\|') or '—'


def _render_exhaustive_answer(
    introduction: str,
    products: list[dict],
    *,
    total_results: int,
    page: int,
    page_size: int,
    complete_result: bool,
) -> str:
    """Render every PostgreSQL product row without asking an LLM to reproduce it."""
    if not products:
        return introduction

    lines = [
        introduction,
        '',
        '| # | Product Name | Product Code | Category |',
        '|---:|---|---|---|',
    ]
    start = (page - 1) * page_size
    for offset, product in enumerate(products, 1):
        lines.append(
            '| {number} | {name} | {code} | {category} |'.format(
                number=start + offset,
                name=_markdown_cell(product.get('product_name')),
                code=_markdown_cell(product.get('product_code')),
                category=_markdown_cell(product.get('category')),
            )
        )

    first = start + 1
    last = start + len(products)
    lines.extend(['', f'Showing {first}-{last} of {total_results} matching products.'])
    if complete_result:
        lines.append('Complete result for the selected catalog scope.')
    return '\n'.join(lines)


def _render_verified_inventory_table(products: list[dict]) -> str:
    if not products:
        return ''
    lines = [
        '### Verified PostgreSQL inventory',
        '',
        '| # | Product Family | Product Code | Category |',
        '|---:|---|---|---|',
    ]
    for number, product in enumerate(products, 1):
        lines.append(
            f"| {number} | {_markdown_cell(product['product_name'])} | "
            f"{_markdown_cell(product['product_code'])} | {_markdown_cell(product['category'])} |"
        )
    return '\n'.join(lines)


def _append_planned_result_status(
    answer: str,
    *,
    complete_result: bool,
    missing_tasks: list[str],
    has_exhaustive_inventory: bool,
) -> str:
    """Make complex-plan coverage authoritative instead of model-generated."""
    # Remove a model-created trailing completeness section if a provider does
    # not follow the prompt. Application evidence is the source of truth.
    answer = re.sub(
        r'\n#{1,6}\s+(?:notes?\s+on\s+)?completeness\b.*\Z',
        '',
        answer,
        flags=re.IGNORECASE | re.DOTALL,
    ).rstrip()
    filtered_lines = []
    for line in answer.splitlines():
        normalized = re.sub(r'[*_`]', '', line.lower())
        if complete_result and re.search(r'\b(not exhaustive|incomplete result|result is partial)\b', normalized):
            continue
        if not complete_result and re.search(r'\b(result is complete|complete result|fully exhaustive)\b', normalized):
            continue
        filtered_lines.append(line)
    answer = '\n'.join(filtered_lines).rstrip()
    lines = [answer, '', '### Result status']
    if complete_result:
        if has_exhaustive_inventory:
            lines.append('Complete for the selected catalog scope and planned inventory filters.')
        else:
            lines.append('All planned retrieval tasks returned supporting evidence.')
    else:
        lines.append('Partial result: one or more planned retrieval tasks returned no evidence.')
        if missing_tasks:
            lines.append('Missing planned evidence: ' + '; '.join(missing_tasks) + '.')
    return '\n'.join(lines)


def _normalized_constraint_text(value: str) -> str:
    text = value.lower().replace('-', ' ')
    text = re.sub(r'\blbs\b', 'lb', text)
    text = re.sub(r'\binches\b', 'inch', text)
    return re.sub(r'\s+', ' ', text).strip()


def _missing_answer_constraints(answer: str, constraints: list[str]) -> list[str]:
    normalized_answer = _normalized_constraint_text(answer)
    return [
        constraint for constraint in constraints
        if _normalized_constraint_text(constraint) not in normalized_answer
    ]


def _markdown_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip('|').split('|')]


def _variant_rows_from_chunks(chunks: list[dict]) -> list[dict]:
    """Parse numeric ordering rows used for deterministic extrema selection."""
    rows: dict[str, dict] = {}
    for chunk in chunks:
        lines = str(chunk.get('text') or '').splitlines()
        metadata = chunk.get('metadata') or {}
        for index, line in enumerate(lines):
            if not line.lstrip().startswith('|'):
                continue
            headers = [re.sub(r'[^A-Z0-9]+', ' ', cell.upper()).strip() for cell in _markdown_cells(line)]
            try:
                cat_index = next(i for i, value in enumerate(headers) if value in {'CAT NR', 'CAT NO'})
                weight_index = next(i for i, value in enumerate(headers) if value == 'HEAD WEIGHT LBS')
            except StopIteration:
                continue
            order_index = next((i for i, value in enumerate(headers) if value in {'ORD NR', 'ORD NO'}), None)
            length_index = next((i for i, value in enumerate(headers) if value == 'OVERALL LENGTH INCH'), None)
            data_index = index + 2
            while data_index < len(lines) and lines[data_index].lstrip().startswith('|'):
                cells = _markdown_cells(lines[data_index])
                data_index += 1
                if len(cells) <= max(cat_index, weight_index):
                    continue
                cat_no = re.sub(r'^[★*\s]+', '', cells[cat_index]).strip()
                weight_match = re.search(r'\d+(?:\.\d+)?', cells[weight_index])
                if not cat_no or not weight_match:
                    continue
                length_match = (
                    re.search(r'\d+(?:\.\d+)?', cells[length_index])
                    if length_index is not None and length_index < len(cells)
                    else None
                )
                rows.setdefault(cat_no, {
                    'cat_no': cat_no,
                    'order_no': cells[order_index].strip() if order_index is not None and order_index < len(cells) else '',
                    'weight_lb': float(weight_match.group()),
                    'length_in': float(length_match.group()) if length_match else None,
                    'product_name': metadata.get('product_name', ''),
                    'source_pdf': metadata.get('source_pdf', ''),
                    'page': metadata.get('page_start', metadata.get('page_num', '')),
                })
    return list(rows.values())


def _query_needs_semantic_details(query: str) -> bool:
    return bool(re.search(
        r'\b(details?|variants?|weights?|lengths?|sizes?|dimensions?|specifications?|'
        r'catalog numbers?|order numbers?|options?)\b',
        query,
        flags=re.IGNORECASE,
    ))


def _query_needs_verified_variant_table(query: str) -> bool:
    return bool(re.search(
        r'\b(variants?|catalog numbers?|catalog codes?|order numbers?|order codes?|'
        r'all available weights?|all weights?)\b',
        query,
        flags=re.IGNORECASE,
    ))


def _inventory_attribute_terms(task) -> list[str]:
    text = f'{task.purpose} {task.query}'.lower().replace('-', ' ')
    return [term for term in ('indestructible',) if term in text]


def _filter_inventory_attributes(products: list[dict], terms: list[str]) -> list[dict]:
    if not terms:
        return products
    result = []
    for product in products:
        haystack = ' '.join([
            str(product.get('product_name') or ''),
            str(product.get('product_code') or ''),
            str(product.get('raw_category') or ''),
            str(product.get('description') or ''),
            str(product.get('features') or ''),
            str(product.get('utilities') or ''),
            str(product.get('materials') or ''),
        ]).lower().replace('-', ' ')
        if all(term in haystack for term in terms):
            result.append(product)
    return result


def _requested_materials(query: str) -> list[str]:
    query_text = query.lower()
    return [
        material for material in ('copper', 'brass')
        if re.search(rf'\b{material}\b', query_text)
    ]


def _row_matches_material(row: dict, material: str) -> bool:
    code = str(row.get('cat_no') or '').upper()
    if code.endswith('/CU'):
        return material == 'copper'
    if code.endswith('/BR'):
        return material == 'brass'
    return material in str(row.get('product_name') or '').lower()


def _rows_relevant_to_query(rows: list[dict], query: str) -> list[dict]:
    query_text = query.lower()
    requested_materials = _requested_materials(query)
    if not requested_materials:
        filtered = rows
    else:
        filtered = []
        for row in rows:
            if any(_row_matches_material(row, material) for material in requested_materials):
                filtered.append(row)
        filtered = filtered or rows

    explicitly_exhaustive = bool(re.search(
        r'\b(?:all|every)\b.{0,50}\b(?:variants?|famil(?:y|ies))\b',
        query_text,
    ))
    requested_weights = {
        float(value)
        for value in re.findall(r'\b(\d+(?:\.\d+)?)\s*(?:lb|lbs)\b', query_text)
    }
    if requested_weights and not explicitly_exhaustive:
        weight_filtered = [row for row in filtered if row['weight_lb'] in requested_weights]
        if weight_filtered:
            filtered = weight_filtered
    return filtered


def _render_verified_variant_table(chunks: list[dict], query: str = '') -> str:
    rows = sorted(
        _rows_relevant_to_query(_variant_rows_from_chunks(chunks), query),
        key=lambda row: (
            str(row.get('product_name') or '').lower(),
            row['weight_lb'],
            row['length_in'] if row['length_in'] is not None else float('inf'),
            row['cat_no'],
        ),
    )
    if not rows:
        return ''
    lines = [
        '### Verified variant and order data',
        '',
        '| Product Family | CAT NR. | ORD NR. | Weight (lb) | Length (inch) |',
        '|---|---|---:|---:|---:|',
    ]
    for row in rows:
        length_text = f"{row['length_in']:g}" if row['length_in'] is not None else ''
        lines.append(
            f"| {_markdown_cell(row['product_name'])} | {_markdown_cell(row['cat_no'])} | "
            f"{_markdown_cell(row['order_no'])} | {row['weight_lb']:g} | {_markdown_cell(length_text)} |"
        )
    return '\n'.join(lines)


def _strip_model_variant_tables(answer: str) -> str:
    lines = answer.splitlines()
    result = []
    index = 0
    while index < len(lines):
        line = lines[index]
        normalized = re.sub(r'[^A-Z0-9]+', ' ', line.upper())
        has_catalog_code = (
            'CAT NR' in normalized
            or 'CATALOG NUMBER' in normalized
            or 'PRODUCT CODE' in normalized
        )
        has_order_code = 'ORD NR' in normalized or 'ORDER NUMBER' in normalized
        if line.lstrip().startswith('|') and has_catalog_code and has_order_code:
            index += 1
            while index < len(lines) and lines[index].lstrip().startswith('|'):
                index += 1
            continue
        result.append(line)
        index += 1
    return '\n'.join(result).strip()


def _remove_unverified_variant_code_lines(answer: str, chunks: list[dict]) -> str:
    allowed = {row['cat_no'].upper() for row in _variant_rows_from_chunks(chunks)}
    if not allowed:
        return answer
    code_pattern = re.compile(r'\b[A-Z][A-Z0-9-]*(?:/[A-Z0-9.-]+)+\b')
    result = []
    for line in answer.splitlines():
        mentioned = {value.upper() for value in code_pattern.findall(line.upper())}
        if mentioned and not mentioned.issubset(allowed):
            continue
        result.append(line)
    return '\n'.join(result).strip()


def _apply_verified_variant_table(answer: str, chunks: list[dict], table: str) -> str:
    if not table:
        return answer
    answer = _strip_model_variant_tables(answer)
    answer = _remove_unverified_variant_code_lines(answer, chunks)
    return f'{answer}\n\n{table}'


def _strip_model_constraint_selections(answer: str) -> str:
    """Remove model-owned selections when application code owns the extrema."""
    return re.sub(
        r'\n?#{1,6}\s+(?:selected\s+options?|proposed\b[^\n]*\bkit|'
        r'recommended?\s+(?:options?|selection))\b.*\Z',
        '',
        answer,
        flags=re.IGNORECASE | re.DOTALL,
    ).rstrip()


def _strip_empty_markdown_headings(answer: str) -> str:
    lines = answer.splitlines()
    cleaned = []
    for index, line in enumerate(lines):
        if not re.match(r'^#{1,6}\s+\S', line.strip()):
            cleaned.append(line)
            continue
        next_content = next(
            (candidate.strip() for candidate in lines[index + 1:] if candidate.strip()),
            '',
        )
        if not next_content or next_content.startswith('#'):
            continue
        cleaned.append(line)
    return '\n'.join(cleaned).strip()


def _render_constraint_matches(
    chunks: list[dict],
    constraints: list[str],
    query: str = '',
) -> str:
    rows = _rows_relevant_to_query(_variant_rows_from_chunks(chunks), query)
    if not rows:
        return ''

    selected: list[tuple[str, dict]] = []
    normalized_constraints = [_normalized_constraint_text(value) for value in constraints]
    for original, normalized in zip(constraints, normalized_constraints):
        combined_match = re.fullmatch(
            r'(shortest|longest)\s+(\d+(?:\.\d+)?)\s*lb',
            normalized,
        )
        if combined_match:
            direction, weight_value = combined_match.groups()
            requested_weight = float(weight_value)
            matches = [
                row for row in rows
                if row['weight_lb'] == requested_weight and row['length_in'] is not None
            ]
            if matches:
                selector = min if direction == 'shortest' else max
                selected.append((original, selector(matches, key=lambda row: row['length_in'])))
        light_length_match = re.fullmatch(r'light\s+(\d+(?:\.\d+)?)\s*inch', normalized)
        if light_length_match:
            requested_length = float(light_length_match.group(1))
            matches = [row for row in rows if row['length_in'] == requested_length]
            if matches:
                selected.append((original, min(matches, key=lambda row: row['weight_lb'])))
        if normalized == 'heavy long handle':
            matches = [row for row in rows if row['length_in'] is not None]
            if matches:
                max_length = max(row['length_in'] for row in matches)
                longest = [row for row in matches if row['length_in'] == max_length]
                selected.append((original, max(longest, key=lambda row: row['weight_lb'])))
    if 'lightest' in normalized_constraints:
        selected.append(('Lightest', min(rows, key=lambda row: row['weight_lb'])))
    if 'heaviest' in normalized_constraints:
        selected.append(('Heaviest', max(rows, key=lambda row: row['weight_lb'])))
    if 'shortest' in normalized_constraints:
        length_rows = [row for row in rows if row['length_in'] is not None]
        if length_rows:
            selected.append(('Shortest', min(length_rows, key=lambda row: row['length_in'])))
    if 'longest' in normalized_constraints:
        length_rows = [row for row in rows if row['length_in'] is not None]
        if length_rows:
            selected.append(('Longest', max(length_rows, key=lambda row: row['length_in'])))
    for original, normalized in zip(constraints, normalized_constraints):
        weight_match = re.fullmatch(r'(\d+(?:\.\d+)?)\s*lb', normalized)
        if not weight_match:
            continue
        requested_weight = float(weight_match.group(1))
        matches = [row for row in rows if row['weight_lb'] == requested_weight]
        if matches:
            requested_materials = _requested_materials(query)
            if len(requested_materials) > 1:
                for material in requested_materials:
                    material_rows = [
                        row for row in matches
                        if _row_matches_material(row, material)
                    ]
                    if material_rows:
                        selected.append((f'{original} {material}', min(
                            material_rows,
                            key=lambda row: row['length_in'] if row['length_in'] is not None else float('inf'),
                        )))
            else:
                selected.append((original, min(
                    matches,
                    key=lambda row: row['length_in'] if row['length_in'] is not None else float('inf'),
                )))

    if not selected:
        return ''
    lines = [
        '### Deterministic constraint matches',
        '',
        '| Requirement | CAT NR. | ORD NR. | Weight (lb) | Length (inch) |',
        '|---|---|---:|---:|---:|',
    ]
    for label, row in selected:
        length_text = f"{row['length_in']:g}" if row['length_in'] is not None else ''
        lines.append(
            f"| {_markdown_cell(label)} | {_markdown_cell(row['cat_no'])} | "
            f"{_markdown_cell(row['order_no'])} | {row['weight_lb']:g} | "
            f"{_markdown_cell(length_text)} |"
        )
    return '\n'.join(lines)


def _apply_deterministic_constraint_matches(
    answer: str,
    chunks: list[dict],
    constraints: list[str],
    query: str = '',
) -> str:
    table = _render_constraint_matches(chunks, constraints, query)
    if not table:
        return answer
    answer = re.sub(
        r'\n#{2,6}\s+Recommendations?\b.*?(?=\n#{2,6}\s+|\Z)',
        '',
        answer,
        flags=re.IGNORECASE | re.DOTALL,
    ).rstrip()
    return f'{answer}\n\n{table}'


class CatalogQueryEngine:
    def __init__(self, runtime: RuntimeModelConfig):
        self.runtime = runtime
        self.answerer = LLMAnswerer(
            provider=runtime.chat_provider,
            api_key=runtime.chat_api_key,
            model=runtime.chat_model,
        )
        self.router = AIQueryRouter(
            provider=runtime.chat_provider,
            api_key=runtime.chat_api_key,
            model=runtime.chat_model,
        )

    def _semantic_retrieve(self, query: str, scope: QueryScope, *, top_k: int = 5) -> list[dict]:
        retriever = HybridRetriever(top_k=top_k)
        return retriever.retrieve(query, query_filter=_scope_filter(scope))

    def _normalize_planned_tasks(self, plan: QueryPlan) -> QueryPlan:
        """Normalize inventory filters while preserving the router's route choice."""
        normalized_tasks = []
        for task in plan.tasks:
            if task.route == QueryRoute.POSTGRES_INVENTORY:
                task_text = f'{task.purpose} {task.query}'.lower().replace('-', ' ')
                categories = [
                    category for category in task.categories
                    if resolve_category_term(category) is not None
                ]
                if not categories and re.search(r'\bhammers?\b', task_text):
                    categories = ['hammer']
                materials = list(task.materials)
                for material in ('copper', 'brass'):
                    if material in task_text and material not in materials:
                        materials.append(material)
                normalized_tasks.append(task.model_copy(update={
                    'categories': categories,
                    'materials': materials,
                }))
                continue
            if task.route != QueryRoute.POSTGRES_EXACT:
                normalized_tasks.append(task)
                continue
            valid_codes = resolve_product_code_tokens(task.product_codes, scope=plan.scope)
            if valid_codes:
                normalized_tasks.append(task.model_copy(update={'product_codes': valid_codes}))
            else:
                normalized_tasks.append(task)
        return plan.model_copy(update={
            'tasks': normalized_tasks,
            'subqueries': [task.query for task in normalized_tasks],
        })

    def _execute_planned_tasks(
        self,
        plan: QueryPlan,
    ) -> tuple[list[dict], list[str], list[dict]]:
        result_sets: list[tuple[str, list[dict]]] = []
        pinned_inventory_sources: list[dict] = []
        verified_inventory: dict[str, dict] = {}
        for task in plan.tasks:
            task_label = _task_label(task)
            if task.route == QueryRoute.POSTGRES_INVENTORY:
                products = list_products_filtered(
                    categories=task.categories,
                    materials=task.materials,
                    scope=plan.scope,
                )
                attribute_terms = _inventory_attribute_terms(task)
                products = _filter_inventory_attributes(products, attribute_terms)
                for product in products:
                    verified_inventory[product['family_id']] = product
                inventory_sources = source_chunks_for_products(products)
                if task.materials or attribute_terms:
                    pinned_inventory_sources.extend(inventory_sources)
                results = [
                    *inventory_sources,
                    *[product_to_chunk(product) for product in products],
                ]
            elif task.route == QueryRoute.POSTGRES_EXACT:
                valid_codes = resolve_product_code_tokens(task.product_codes, scope=plan.scope)
                if valid_codes:
                    products = find_by_product_codes(valid_codes, scope=plan.scope)
                    results = [product_to_chunk(product) for product in products]
                else:
                    results = []
            else:
                results = self._semantic_retrieve(task.query, plan.scope, top_k=5)
            result_sets.append((task_label, results))
        merged, missing = _merge_subquery_results(result_sets, limit=25)

        # Inventory source Markdown contains the reviewed variant/order tables.
        # RRF may otherwise push these authoritative rows below the fusion cap
        # when a complex plan also has several semantic tasks. Keep each source
        # chunk available to the answer validator without changing its vectors.
        merged_keys = {_result_identity(result) for result in merged}
        for source in pinned_inventory_sources:
            key = _result_identity(source)
            if key not in merged_keys:
                merged.append(source)
                merged_keys.add(key)
        return merged, missing, list(verified_inventory.values())

    def execute(
        self,
        query: str,
        *,
        catalog_ids: list[str] | None = None,
        document_ids: list[str] | None = None,
        page: int = 1,
        page_size: int = 50,
        memory: dict | None = None,
    ) -> QueryExecution:
        request_scope = QueryScope(
            catalog_ids=catalog_ids or [],
            document_ids=document_ids or [],
            source_types=['catalog'],
        )
        analysis_query = query.strip() or query
        try:
            router_result = self.router.plan(query, scope=request_scope, memory=memory)
            routed_plan = router_result.plan
            plan = routed_plan.to_query_plan()
            analysis_query = routed_plan.standalone_query or analysis_query
        except AIQueryRouterError as error:
            logger.warning(
                'AI query router failed; falling back to deterministic planner. query=%r error=%s',
                query,
                error,
            )
            plan = deterministic_plan(query, request_scope)
        scope = plan.scope

        if plan.needs_clarification:
            return QueryExecution(
                plan=plan,
                standalone_query=analysis_query,
                answer=plan.clarification_question,
                chunks=[],
                sources=[],
                complete_result=False,
                total_results=None,
                page=page,
                page_size=page_size,
                missing_entities=[],
            )

        chunks: list[dict] = []
        complete = False
        total: int | None = None
        missing: list[str] = []
        exhaustive_products: list[dict] | None = None
        planned_inventory_products: list[dict] = []

        inventory_task = _inventory_task(plan)
        planned_codes = _planned_product_codes(plan)
        valid_codes = resolve_product_code_tokens(planned_codes, scope=scope)

        if plan.intent == QueryIntent.EXHAUSTIVE_LIST:
            category = _category_entity(plan) or (
                inventory_task.categories[0]
                if inventory_task and inventory_task.categories
                else ''
            )
            if inventory_task:
                products = list_products_filtered(
                    categories=inventory_task.categories,
                    materials=inventory_task.materials,
                    scope=scope,
                )
                total = len(products)
                start = (page - 1) * page_size
                exhaustive_products = products[start:start + page_size]
                chunks = [product_to_chunk(product) for product in exhaustive_products]
                complete = page * page_size >= total
                if total == 0 and category:
                    missing = [category]
            else:
                product_page = list_products(
                    category_value=category,
                    scope=scope,
                    page=page,
                    page_size=page_size,
                )
                chunks = [product_to_chunk(product) for product in product_page.products]
                complete = product_page.complete
                total = product_page.total
                exhaustive_products = product_page.products
                if category and total == 0:
                    missing = [category]

        elif plan.intent == QueryIntent.AGGREGATION:
            category = _category_entity(plan) or (
                inventory_task.categories[0]
                if inventory_task and inventory_task.categories
                else ''
            )
            if inventory_task:
                total = len(list_products_filtered(
                    categories=inventory_task.categories,
                    materials=inventory_task.materials,
                    scope=scope,
                ))
            else:
                total = list_products(
                    category_value=category,
                    scope=scope,
                    page=1,
                    page_size=page_size,
                ).total
            label = category or 'products'
            chunks = [{
                'text': f'Total active {label}: {total}',
                'metadata': {
                    'product_name': f'{label} count',
                    'product_code': '',
                    'source_pdf': 'PostgreSQL inventory',
                },
                'score': 1.0,
            }]
            complete = True

        elif plan.intent == QueryIntent.EXACT_LOOKUP:
            products = find_by_product_codes(valid_codes, scope=scope) if valid_codes else []
            chunks = [product_to_chunk(product) for product in products]
            if products and _query_needs_semantic_details(analysis_query):
                semantic_chunks = self._semantic_retrieve(analysis_query, scope, top_k=5)
                chunks, _ = _merge_subquery_results([
                    ('Structured exact match', chunks),
                    ('Catalog specification details', semantic_chunks),
                ], limit=10)
            complete = bool(products)
            total = len(products)
            if not products:
                missing = planned_codes or [analysis_query]

        elif plan.intent == QueryIntent.COMPARISON:
            products = find_by_product_codes(valid_codes, scope=scope) if valid_codes else []
            chunks = [product_to_chunk(product) for product in products]
            if products and _query_needs_semantic_details(analysis_query):
                semantic_chunks = self._semantic_retrieve(analysis_query, scope, top_k=5)
                chunks, _ = _merge_subquery_results([
                    ('Structured comparison matches', chunks),
                    ('Catalog comparison details', semantic_chunks),
                ], limit=12)
            found_codes = {
                value.upper()
                for product in products
                for value in (
                    product['product_code'],
                    *[
                        code
                        for variant in product['variants']
                        for code in (
                            variant['product_code'],
                            variant['order_number'],
                            variant['name'],
                        )
                    ],
                )
                if value
            }
            if planned_codes:
                missing = [code for code in planned_codes if code.upper() not in found_codes]
                complete = not missing
            else:
                missing = [analysis_query]
                complete = False
            total = len(products)

        elif plan.tasks:
            chunks, missing, planned_inventory_products = self._execute_planned_tasks(plan)
            complete = not missing
            total = len(chunks)

        elif plan.subqueries:
            subqueries = plan.subqueries or [
                part.strip()
                for part in re.split(r'[,;]|\band\b', analysis_query)
                if part.strip()
            ][:5]
            result_sets = [
                (subquery, self._semantic_retrieve(subquery, scope, top_k=3))
                for subquery in subqueries
            ]
            chunks, missing = _merge_subquery_results(result_sets)
            complete = bool(subqueries) and not missing
            total = len(chunks)

        else:
            chunks = self._semantic_retrieve(analysis_query, scope, top_k=5)
            total = len(chunks)

        verified_variant_table = (
            _render_verified_variant_table(chunks, analysis_query)
            if _query_needs_verified_variant_table(analysis_query)
            else ''
        )
        verified_inventory_table = _render_verified_inventory_table(planned_inventory_products)
        requested_constraints = extract_requested_constraints(analysis_query)
        deterministic_constraint_table = (
            _render_constraint_matches(chunks, requested_constraints, analysis_query)
            if plan.used_model_planner
            else ''
        )
        evidence = {
            'complete_result': complete,
            'total_results': total,
            'page': page,
            'page_size': page_size,
            'missing_entities': missing,
            'planned_tasks': [task.purpose for task in plan.tasks],
            'required_constraints': requested_constraints,
            'application_variant_table': bool(verified_variant_table),
            'application_inventory_table': bool(verified_inventory_table),
            'application_constraint_table': bool(deterministic_constraint_table),
        }
        if plan.intent == QueryIntent.EXHAUSTIVE_LIST and exhaustive_products is not None:
            introduction = self.answerer.exhaustive_introduction(
                analysis_query,
                returned_count=len(exhaustive_products),
                total_results=total or 0,
                page=page,
                complete_result=complete,
            )
            answer = _render_exhaustive_answer(
                introduction,
                exhaustive_products,
                total_results=total or 0,
                page=page,
                page_size=page_size,
                complete_result=complete,
            )
        elif plan.intent == QueryIntent.AGGREGATION:
            label = _category_entity(plan) or (
                inventory_task.categories[0]
                if inventory_task and inventory_task.categories
                else 'product'
            )
            answer = (
                f'There are {total or 0} active {label} product families '
                'in the selected catalog scope.'
            )
        else:
            answer = self.answerer.answer(analysis_query, chunks, evidence=evidence)
            if deterministic_constraint_table:
                answer = _strip_model_constraint_selections(answer)
            answer = _apply_verified_variant_table(
                answer,
                chunks,
                verified_variant_table,
            )
            if verified_inventory_table:
                answer = f'{answer}\n\n{verified_inventory_table}'
            answer = _strip_empty_markdown_headings(answer)
            if plan.used_model_planner:
                answer = _apply_deterministic_constraint_matches(
                    answer,
                    chunks,
                    evidence['required_constraints'],
                    analysis_query,
                )
                missing_constraints = _missing_answer_constraints(
                    answer,
                    evidence['required_constraints'],
                )
                if missing_constraints:
                    retry_evidence = {
                        **evidence,
                        'missing_entities': [
                            *missing,
                            *[f'required answer constraint: {value}' for value in missing_constraints],
                        ],
                    }
                    answer = self.answerer.answer(
                        analysis_query + '\n\nExplicitly address these required constraints: '
                        + ', '.join(missing_constraints),
                        chunks,
                        evidence=retry_evidence,
                    )
                    if deterministic_constraint_table:
                        answer = _strip_model_constraint_selections(answer)
                    answer = _apply_verified_variant_table(
                        answer,
                        chunks,
                        verified_variant_table,
                    )
                    if verified_inventory_table:
                        answer = f'{answer}\n\n{verified_inventory_table}'
                    answer = _strip_empty_markdown_headings(answer)
                    answer = _apply_deterministic_constraint_matches(
                        answer,
                        chunks,
                        evidence['required_constraints'],
                        analysis_query,
                    )
                    missing_constraints = _missing_answer_constraints(
                        answer,
                        evidence['required_constraints'],
                    )
                if missing_constraints:
                    complete = False
                    missing.extend(
                        f'required answer constraint: {value}'
                        for value in missing_constraints
                    )
                answer = _append_planned_result_status(
                    answer,
                    complete_result=complete,
                    missing_tasks=missing,
                    has_exhaustive_inventory=any(
                        task.exhaustive or task.route == QueryRoute.POSTGRES_INVENTORY
                        for task in plan.tasks
                    ),
                )
        sources = []
        seen_sources = set()
        for chunk in chunks:
            metadata = chunk.get('metadata', {})
            source = {
                'name': metadata.get('product_name', ''),
                'code': metadata.get('product_code', ''),
                'pdf': metadata.get('source_pdf', ''),
                'page_start': metadata.get('page_start', metadata.get('page_num', 0)),
                'page_end': metadata.get('page_end', metadata.get('page_num', 0)),
                'catalog': metadata.get('catalog', ''),
                'score': chunk.get('score', 0),
            }
            key = (source['code'], source['pdf'], source['page_start'], source['name'])
            if key not in seen_sources:
                sources.append(source)
                seen_sources.add(key)

        return QueryExecution(
            plan=plan,
            standalone_query=analysis_query,
            answer=answer,
            chunks=chunks,
            sources=sources,
            complete_result=complete,
            total_results=total,
            page=page,
            page_size=page_size,
            missing_entities=missing,
        )
