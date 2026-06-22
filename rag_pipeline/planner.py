"""English query routing with deterministic fast paths and validated LLM plans."""

from __future__ import annotations

import re
from enum import Enum
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
# from langchain_openai import ChatOpenAI  # disabled; using Groq
from langchain_groq import ChatGroq
from pydantic import BaseModel, Field, field_validator


class QueryIntent(str, Enum):
    EXACT_LOOKUP = 'exact_lookup'
    EXHAUSTIVE_LIST = 'exhaustive_list'
    AGGREGATION = 'aggregation'
    COMPARISON = 'comparison'
    RECOMMENDATION = 'recommendation'
    TROUBLESHOOTING = 'troubleshooting'
    MULTI_INTENT = 'multi_intent'
    GENERAL_SEMANTIC = 'general_semantic'
    AMBIGUOUS = 'ambiguous'


class QueryRoute(str, Enum):
    POSTGRES_INVENTORY = 'postgres_inventory'
    POSTGRES_EXACT = 'postgres_exact'
    HYBRID_SEARCH = 'hybrid_search'


class QueryEntity(BaseModel):
    type: str
    value: str


class QueryTask(BaseModel):
    route: QueryRoute
    purpose: str
    query: str
    product_codes: list[str] = Field(default_factory=list, max_length=20)
    categories: list[str] = Field(default_factory=list, max_length=10)
    materials: list[str] = Field(default_factory=list, max_length=10)
    exhaustive: bool = False


class QueryConstraint(BaseModel):
    name: str
    value: str


class QueryScope(BaseModel):
    catalog_ids: list[str] = Field(default_factory=list)
    document_ids: list[str] = Field(default_factory=list)
    source_types: list[str] = Field(default_factory=lambda: ['catalog'])

    @field_validator('source_types')
    @classmethod
    def valid_source_types(cls, values):
        allowed = {'catalog', 'manual'}
        result = [value for value in values if value in allowed]
        return result or ['catalog']


class QueryPlan(BaseModel):
    intent: QueryIntent
    intents: list[QueryIntent] = Field(default_factory=list, max_length=5)
    scope: QueryScope = Field(default_factory=QueryScope)
    entities: list[QueryEntity] = Field(default_factory=list)
    constraints: dict[str, Any] = Field(default_factory=dict)
    subqueries: list[str] = Field(default_factory=list, max_length=5)
    tasks: list[QueryTask] = Field(default_factory=list, max_length=5)
    needs_clarification: bool = False
    clarification_question: str = ''
    used_model_planner: bool = False


class StructuredQueryPlan(BaseModel):
    intents: list[QueryIntent] = Field(min_length=1, max_length=5)
    tasks: list[QueryTask] = Field(min_length=1, max_length=5)
    entities: list[QueryEntity] = Field(default_factory=list, max_length=20)
    constraints: list[QueryConstraint] = Field(default_factory=list, max_length=20)
    needs_clarification: bool = False
    clarification_question: str = ''


CODE_PATTERN = re.compile(
    r'\b(?:[A-Za-z]{1,12}(?:[-/][A-Za-z0-9.]+)+|[A-Za-z]{2,12}-\d[A-Za-z0-9/-]*)\b'
)


def extract_product_codes(query: str) -> list[str]:
    codes = []
    for match in CODE_PATTERN.findall(query):
        code = match.strip('.,:;()[]{}').upper()
        if code not in codes:
            codes.append(code)
    return codes


def _category_phrase(query: str) -> str:
    text = query.lower().strip()
    patterns = (
        r'^how many\s+(.+?)[?.!]*$',
        r'^what\s+(.+?)[?.!]*$',
        r'^(?:list|show(?:\s+me)?)\s+(?:all\s+)?(.+?)(?:\s+available)?(?:\s+in\s+(?:this|the|current)\s+catalog)?[?.!]*$',
        r'^(?:all|every)\s+(.+?)(?:\s+are\s+available)?(?:\s+in\s+(?:this|the|current)\s+catalog)?[?.!]*$',
        r'\btypes?\s+of\s+(?:the\s+)?(.+?)[?.!]*$',
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            value = re.sub(
                r'\s+(?:are\s+)?available(?:\s+(?:in|from)\s+(?:(?:this|the|current)\s+)*catalog(?:\s+scope)?)?$',
                '',
                match.group(1),
            )
            value = re.sub(r'\b(types?\s+of|product\s+famil(?:y|ies)|products?|tools?|available|catalog|the)\b', ' ', value)
            value = re.sub(r'\s+', ' ', value).strip(' ?.!')
            if value:
                return value
    return ''


def deterministic_plan(query: str, scope: QueryScope | None = None) -> QueryPlan:
    query = query.strip()
    scope = scope or QueryScope()
    lowered = query.lower()
    codes = extract_product_codes(query)
    entities = [QueryEntity(type='product_code', value=code) for code in codes]

    if re.search(r'\b(compare|comparison|versus|vs\.?|difference between)\b', lowered):
        return QueryPlan(intent=QueryIntent.COMPARISON, scope=scope, entities=entities)

    if re.search(r'\b(how many|count|total number)\b', lowered):
        category = _category_phrase(query)
        if category:
            entities.append(QueryEntity(type='category', value=category))
        return QueryPlan(intent=QueryIntent.AGGREGATION, scope=scope, entities=entities)

    if codes and re.search(r'\b(show|find|details?|specifications?|specs?|information|what is)\b', lowered):
        return QueryPlan(intent=QueryIntent.EXACT_LOOKUP, scope=scope, entities=entities)

    if re.search(r'\b(all|every|available|types? of|list)\b', lowered):
        category = _category_phrase(query)
        if category:
            entities.append(QueryEntity(type='category', value=category))
        return QueryPlan(intent=QueryIntent.EXHAUSTIVE_LIST, scope=scope, entities=entities)

    if re.search(r'\b(kit|bundle|combine|each of|following items?|multi-product)\b', lowered):
        return QueryPlan(intent=QueryIntent.MULTI_INTENT, scope=scope, entities=entities)

    if re.search(r'\b(recommend|suitable|best|which (?:tool|product)|need a|looking for)\b', lowered):
        return QueryPlan(intent=QueryIntent.RECOMMENDATION, scope=scope, entities=entities)

    if re.search(r'\b(not working|failure|fails?|repair|troubleshoot|install|replace|why (?:does|is|will))\b', lowered):
        scope.source_types = ['manual']
        return QueryPlan(intent=QueryIntent.TROUBLESHOOTING, scope=scope, entities=entities)

    if len(query.split()) <= 2 and not codes:
        return QueryPlan(
            intent=QueryIntent.AMBIGUOUS,
            scope=scope,
            entities=entities,
            needs_clarification=True,
            clarification_question='What product or specification would you like to find?',
        )
    return QueryPlan(intent=QueryIntent.GENERAL_SEMANTIC, scope=scope, entities=entities)


PLANNER_PROMPT = """You plan English GROZ catalog searches using a strict schema.

Use at most five focused tasks. Available routes:
- postgres_inventory: exhaustive family inventory by normalized category/material.
- postgres_exact: exact product/order-code lookup only when the user explicitly supplied a real code.
- hybrid_search: specifications, suitability, recommendations, descriptions, or variant tables from catalog text.

Rules:
- A request may have multiple intents. Preserve all of them.
- Hyphenated properties such as non-sparking, heavy-duty, and soft-face are NOT product codes.
- Never invent a product code. Only copy a code explicitly present in the user query.
- `available` does not mean exhaustive when it modifies options, weights, sizes, handles, or variants.
- `maintenance` means manual troubleshooting only when the user asks to install, repair, diagnose, or service something; a maintenance team buying products is a catalog request.
- For exhaustive+comparison+recommendation requests, create an inventory task plus focused hybrid-search tasks.
- Put all recommendation constraints (for example lightest, 4 lb, and heaviest) into one recommendation task so none are dropped.
- For a kit/bundle containing named product families, create one hybrid_search task per requested family; do not replace the kit with a generic inventory task.
- Requests for all variants, weights, lengths, catalog numbers, or order numbers of one named family need hybrid_search, not postgres_inventory.
- Preserve every number, unit, material, category, and requested constraint exactly.
- Do not answer the query and do not generate SQL.

Example: a request to list every copper/brass hammer family, compare non-sparking suitability, and recommend lightest/4 lb/heaviest options should create:
1. postgres_inventory for hammer/sledge-hammer with copper/brass materials;
2. hybrid_search for all copper variants and order codes;
3. hybrid_search for all brass variants and order codes;
4. hybrid_search for non-sparking suitability;
5. hybrid_search for the lightest, 4 lb, and heaviest constraints.
"""


def extract_requested_constraints(query: str) -> list[str]:
    """Extract explicit constraints that must survive planning and answering."""
    matches: list[tuple[int, str]] = []
    covered_spans = []
    for match in re.finditer(
        r'\b(?:shortest|longest)\s+\d+(?:\.\d+)?\s*(?:lb|lbs|kg|g|mm|cm|m|inch|inches|in)\b',
        query,
        flags=re.IGNORECASE,
    ):
        matches.append((match.start(), match.group(0).strip()))
        covered_spans.append(match.span())
    patterns = (
        r'\b(?:light|heavy)\s+(?:\d+(?:\.\d+)?\s*(?:lb|lbs|kg|g|mm|cm|m|inch|inches|in)|long[- ]handle)\b',
        r'\b(?:lightest|heaviest|shortest|longest|smallest|largest)\b',
        r'\b\d+(?:\.\d+)?\s*(?:lb|lbs|kg|g|mm|cm|m|inch|inches|in)\b',
        r'\bnon[- ]sparking\b',
    )
    for pattern in patterns:
        for match in re.finditer(pattern, query, flags=re.IGNORECASE):
            if any(match.start() >= start and match.end() <= end for start, end in covered_spans):
                continue
            matches.append((match.start(), match.group(0).strip()))
            covered_spans.append(match.span())
    return list(dict.fromkeys(value for _, value in sorted(matches)))


def _fallback_multi_item_queries(query: str) -> list[str]:
    """Recover named kit/items when a model collapses them into one task."""
    match = re.search(
        r'(?:containing|following items?\s*:|kit\s+with|bundle\s+with)\s+(.+)',
        query,
        flags=re.IGNORECASE,
    )
    if not match:
        return [query]
    value = re.split(r'\.\s*(?:give|show|include|provide)\b', match.group(1), maxsplit=1, flags=re.IGNORECASE)[0]
    parts = re.split(r'\s*,\s*|\s+and\s+', value, flags=re.IGNORECASE)
    cleaned = []
    for part in parts:
        item = re.sub(r'^(?:a|an|the|and)\s+', '', part.strip(' .;:'), flags=re.IGNORECASE)
        if item and item not in cleaned:
            cleaned.append(item)
    return cleaned[:5] or [query]


def should_use_model_planner(query: str, plan: QueryPlan, *, valid_product_codes: list[str]) -> bool:
    """Keep obvious fast paths local and send compositional requests to the LLM."""
    lowered = query.lower()
    signals = set()
    if re.search(r'\b(compare|comparison|versus|vs\.?|difference between)\b', lowered):
        signals.add('comparison')
    if re.search(r'\b(all|every|types? of|list)\b', lowered) or re.search(r'\bwhat\s+.+?\s+are available\b', lowered):
        signals.add('exhaustive')
    if re.search(r'\b(recommend|suitable|best|which (?:tool|product)|need a|looking for)\b', lowered):
        signals.add('recommendation')
    if re.search(r'\b(kit|bundle|combine|each of|following items?|multi-product|first.+second|second.+third)\b', lowered):
        signals.add('multi')
    if re.search(r'\b(how many|count|total number)\b', lowered):
        signals.add('aggregation')
    if re.search(r'\b(not working|failure|fails?|repair|troubleshoot|install|replace|why (?:does|is|will))\b', lowered):
        signals.add('troubleshooting')

    if len(signals) > 1 or plan.intent == QueryIntent.MULTI_INTENT:
        return True
    if plan.intent == QueryIntent.EXHAUSTIVE_LIST and 'exhaustive' not in signals:
        # Words such as "available" can describe variants/options rather than
        # request a complete inventory; let the model resolve that ambiguity.
        return True
    if plan.intent == QueryIntent.EXHAUSTIVE_LIST and re.search(
        r'\b(variants?|weights?|lengths?|sizes?|options?|catalog numbers?|order numbers?)\b',
        lowered,
    ):
        return True
    if plan.intent == QueryIntent.COMPARISON and len(valid_product_codes) < 2:
        return True
    if (
        re.search(r'\b(catalog|order)\s+(?:numbers?|codes?)\b', lowered)
        and re.search(r'\b\d+(?:\.\d+)?\s*(?:lb|lbs|kg|mm|cm|inch|inches|in)\b', lowered)
        and re.search(r'\b(and|or)\b', lowered)
    ):
        return True
    if query.count(';') >= 2 or (re.search(r'\bfirst\b', lowered) and re.search(r'\bsecond\b', lowered)):
        return True
    return False


def enrich_complex_plan(
    plan: QueryPlan,
    query: str,
    *,
    api_key: str,
    model: str = 'gpt-4o-mini',
) -> QueryPlan:
    if not api_key:
        return plan.model_copy(update={
            'intent': QueryIntent.MULTI_INTENT,
            'intents': [plan.intent],
            'subqueries': [query],
            'tasks': [QueryTask(
                route=QueryRoute.HYBRID_SEARCH,
                purpose='Safe fallback search',
                query=query,
            )],
        })

    # llm = ChatOpenAI(model=model, api_key=api_key, temperature=0.0, max_retries=3)  # disabled
    llm = ChatGroq(model=model, api_key=api_key, temperature=0.0, max_retries=3)
    try:
        structured_llm = llm.with_structured_output(
            StructuredQueryPlan,
            method='json_schema',
            strict=True,
        )
        output = structured_llm.invoke([
            SystemMessage(content=PLANNER_PROMPT),
            HumanMessage(content=query),
        ])
    except Exception:
        return plan.model_copy(update={
            'intent': QueryIntent.MULTI_INTENT,
            'intents': [plan.intent],
            'entities': [],
            'subqueries': [query],
            'tasks': [QueryTask(
                route=QueryRoute.HYBRID_SEARCH,
                purpose='Safe fallback search',
                query=query,
            )],
        })

    intents = list(dict.fromkeys(output.intents))
    # Structured output guarantees shape, not semantic route validity. An
    # exact lookup without an explicit code cannot be executed safely, so it
    # becomes a hybrid catalog search before leaving the planner.
    tasks = [
        task.model_copy(update={'route': QueryRoute.HYBRID_SEARCH})
        if task.route == QueryRoute.POSTGRES_EXACT and not task.product_codes
        else task
        for task in output.tasks
    ]
    if plan.intent == QueryIntent.MULTI_INTENT and (
        len(tasks) < 2 or all(task.route == QueryRoute.POSTGRES_INVENTORY for task in tasks)
    ):
        item_queries = _fallback_multi_item_queries(query)
        if len(item_queries) > 1:
            tasks = [
                QueryTask(
                    route=QueryRoute.HYBRID_SEARCH,
                    purpose=f'Find requested item: {item_query}',
                    query=item_query,
                )
                for item_query in item_queries
            ]
            output = output.model_copy(update={'intents': [QueryIntent.MULTI_INTENT]})
    requested_constraints = extract_requested_constraints(query)
    task_text = ' '.join(
        f'{task.purpose} {task.query}' for task in tasks
    ).lower().replace('-', ' ')
    missing_constraints = [
        value for value in requested_constraints
        if value.lower().replace('-', ' ') not in task_text
    ]
    if missing_constraints:
        target_index = next(
            (
                index for index in range(len(tasks) - 1, -1, -1)
                if tasks[index].route == QueryRoute.HYBRID_SEARCH
                and 'recommend' in tasks[index].purpose.lower()
            ),
            next(
                (
                    index for index in range(len(tasks) - 1, -1, -1)
                    if tasks[index].route == QueryRoute.HYBRID_SEARCH
                ),
                None,
            ),
        )
        if target_index is not None:
            target = tasks[target_index]
            suffix = ', '.join(missing_constraints)
            tasks[target_index] = target.model_copy(update={
                'query': f'{target.query}; also address: {suffix}',
            })

    primary_intent = intents[0] if len(intents) == 1 and len(output.tasks) == 1 else QueryIntent.MULTI_INTENT
    constraints = {item.name: item.value for item in output.constraints}
    if requested_constraints:
        constraints['required_terms'] = ', '.join(requested_constraints)
    return plan.model_copy(update={
        'intent': primary_intent,
        'intents': intents,
        'subqueries': [task.query for task in tasks],
        'tasks': tasks,
        'entities': output.entities,
        'constraints': constraints,
        'needs_clarification': output.needs_clarification,
        'clarification_question': output.clarification_question,
        'used_model_planner': True,
    })
