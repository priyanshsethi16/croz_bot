"""Helpers for storing and restoring k=1 chat memory in Django sessions."""

from __future__ import annotations

import re
from typing import Any

from rag_pipeline.ai_router import ConversationTurn
from rag_pipeline.planner import QueryEntity, QueryIntent, QueryScope

PUBLIC_CHAT_MEMORY_KEY = 'rag_last_turn'
ADMIN_CHAT_MEMORY_KEY = 'admin_rag_last_turn'
_SUMMARY_LIMIT = 240
_MAX_SOURCES = 5
_MAX_ENTITIES = 10
_MAX_SCOPE_IDS = 20
_MAX_FIELD_LENGTH = 120


def load_turn(session, key: str) -> ConversationTurn:
    raw = session.get(key, {})
    if not isinstance(raw, dict):
        return ConversationTurn()
    try:
        return ConversationTurn.model_validate(raw)
    except Exception:
        return ConversationTurn()


def dump_turn(turn: ConversationTurn) -> dict[str, Any]:
    return turn.model_dump(mode='json', exclude_defaults=True, exclude_none=True)


def _clean_text(value: Any) -> str:
    text = str(value or '').strip()
    text = re.sub(r'\s+', ' ', text)
    return text


def _compact_text(value: Any, limit: int = _MAX_FIELD_LENGTH) -> str:
    text = _clean_text(value)
    if len(text) > limit:
        text = text[:limit].rstrip()
    return text


def _summarize_answer(answer: Any) -> str:
    text = str(answer or '')
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()
    if not text:
        return ''

    lines: list[str] = []
    in_code_block = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith('```'):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue
        if stripped.startswith('|'):
            continue
        if stripped.startswith('#'):
            stripped = stripped.lstrip('#').strip()
        if not stripped:
            continue
        lines.append(stripped)
        if len(' '.join(lines)) >= _SUMMARY_LIMIT:
            break

    summary = ' '.join(lines).strip() if lines else _clean_text(text)
    if len(summary) > _SUMMARY_LIMIT:
        summary = summary[:_SUMMARY_LIMIT].rstrip()
    return summary


def _compact_entities(values: Any) -> list[QueryEntity]:
    result: list[QueryEntity] = []
    seen: set[tuple[str, str]] = set()
    for value in list(values or []):
        try:
            entity = value if isinstance(value, QueryEntity) else QueryEntity.model_validate(value)
        except Exception:
            continue
        entity_type = _compact_text(entity.type, 40)
        entity_value = _compact_text(entity.value)
        if not entity_type or not entity_value:
            continue
        key = (entity_type.lower(), entity_value.lower())
        if key in seen:
            continue
        seen.add(key)
        result.append(QueryEntity(type=entity_type, value=entity_value))
        if len(result) >= _MAX_ENTITIES:
            break
    return result


def _compact_scope(scope: Any) -> QueryScope:
    try:
        scope_model = scope if isinstance(scope, QueryScope) else QueryScope.model_validate(scope or {})
    except Exception:
        return QueryScope()
    return QueryScope(
        catalog_ids=[_compact_text(value, 80) for value in scope_model.catalog_ids[:_MAX_SCOPE_IDS]],
        document_ids=[_compact_text(value, 80) for value in scope_model.document_ids[:_MAX_SCOPE_IDS]],
        source_types=list(scope_model.source_types),
    )


def _compact_sources(sources: Any) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for source in list(sources or []):
        if not isinstance(source, dict):
            continue
        code = _compact_text(source.get('code', ''), 80)
        name = _compact_text(source.get('name', ''))
        if not code and not name:
            continue
        key = (code.lower(), name.lower())
        if key in seen:
            continue
        seen.add(key)
        entry: dict[str, str] = {}
        if code:
            entry['code'] = code
        if name:
            entry['name'] = name
        result.append(entry)
        if len(result) >= _MAX_SOURCES:
            break
    return result


def _should_skip_memory_update(execution: Any, plan: Any, sources: list[dict[str, str]]) -> bool:
    if plan is not None and getattr(plan, 'needs_clarification', False):
        return True
    if plan is not None and getattr(plan, 'intent', None) == QueryIntent.AMBIGUOUS:
        return True
    missing_entities = list(getattr(execution, 'missing_entities', []) or [])
    if missing_entities and not sources:
        return True
    return False


def build_memory_update(
    query: str,
    execution: Any,
    *,
    standalone_query: str | None = None,
) -> ConversationTurn | None:
    """Build the compact k=1 memory payload for the next query."""
    cleaned_query = _clean_text(query)
    if not cleaned_query or execution is None:
        return None

    plan = getattr(execution, 'plan', None)
    answer = getattr(execution, 'answer', '')
    sources = _compact_sources(getattr(execution, 'sources', []) or [])
    execution_standalone_query = _clean_text(getattr(execution, 'standalone_query', ''))
    if _should_skip_memory_update(execution, plan, sources):
        return None

    subqueries = list(getattr(plan, 'subqueries', []) or []) if plan is not None else []
    last_standalone_query = _compact_text(
        standalone_query
        or execution_standalone_query
        or (subqueries[0] if subqueries else cleaned_query),
        500,
    )

    if plan is None:
        return ConversationTurn(
            last_user_query=cleaned_query,
            last_standalone_query=last_standalone_query,
            last_answer_summary=_summarize_answer(answer),
            last_sources=sources,
        )

    intents = list(getattr(plan, 'intents', []) or [])
    if not intents:
        intents = [getattr(plan, 'intent', QueryIntent.GENERAL_SEMANTIC)]
    intents = intents[:5]

    if not last_standalone_query and subqueries:
        last_standalone_query = _compact_text(subqueries[0], 500)

    return ConversationTurn(
        last_user_query=cleaned_query,
        last_standalone_query=last_standalone_query,
        last_intent=getattr(plan, 'intent', QueryIntent.GENERAL_SEMANTIC),
        last_intents=intents,
        last_entities=_compact_entities(getattr(plan, 'entities', []) or []),
        last_scope=_compact_scope(getattr(plan, 'scope', QueryScope())),
        last_answer_summary=_summarize_answer(answer),
        last_sources=sources,
    )


def build_turn(query: str, execution: Any, *, standalone_query: str | None = None) -> ConversationTurn:
    """Backward-compatible wrapper for older call sites."""
    return build_memory_update(query, execution, standalone_query=standalone_query) or ConversationTurn()


def clear_turn(session, key: str) -> None:
    if key in session:
        session.pop(key, None)
        session.modified = True


def save_turn(session, key: str, turn: ConversationTurn) -> dict[str, Any]:
    payload = dump_turn(turn)
    session[key] = payload
    session.modified = True
    return payload
