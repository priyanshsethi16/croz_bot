"""Memory-aware AI query routing for GROZ catalog search.

CatalogQueryEngine uses this router as the runtime intent planner. The router
returns a validated standalone query, scope, entities, and executable tasks;
application code still owns PostgreSQL/Qdrant execution and answer grounding.
"""

from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rag_pipeline.planner import (
    QueryEntity,
    QueryIntent,
    QueryPlan,
    QueryRoute,
    QueryScope,
    QueryTask,
)


AI_ROUTER_SYSTEM_PROMPT = """You are the routing brain for GROZ catalog search.
Return a strict JSON object and nothing else.

Your job is to plan, not to answer the question.

Rules:
- The current user query is English-only. Keep standalone_query in English too.
- Use the current query as the highest priority.
- Use memory only to resolve follow-up references such as it, that one, same one,
  uski, iski, previous product, or previous item.
- Do not use memory as a general knowledge source or to override explicit details
  in the current query.
- Never invent a product code.
- Preserve explicit product codes, categories, materials, units, and constraints.
- If the user asks for troubleshooting, repair, install, diagnose, or replace
  help, prefer manual scope.
- If the user asks for product lookup, specs, variants, comparisons,
  recommendations, counts, or exhaustive lists, use catalog scope unless the
  query explicitly asks for manuals too.
- Return at most five focused tasks.
- If the request is ambiguous, set needs_clarification=true and keep tasks empty.
- Do not write SQL.
- Do not answer the user's question.

Output keys:
standalone_query, intent, intents, scope, entities, constraints, subqueries,
tasks, needs_clarification, clarification_question, confidence, memory_used
"""

AI_ROUTER_REPAIR_PROMPT = """Your previous response did not match the required JSON schema.
Fix it and return only valid JSON.

Schema requirements:
- standalone_query: string
- intent: one of exact_lookup, exhaustive_list, aggregation, comparison,
  recommendation, troubleshooting, multi_intent, general_semantic, ambiguous
- intents: list of the allowed intents
- scope: object with catalog_ids, document_ids, source_types
- entities: list of objects with type and value
- constraints: object
- subqueries: list of strings
- tasks: list of objects with route, purpose, query, product_codes, categories,
  materials, exhaustive
- needs_clarification: boolean
- clarification_question: string
- confidence: number between 0 and 1
- memory_used: boolean

Original request payload:
{request}

Error:
{error}

Previous output:
{output}
"""


def _clean_text(value: Any) -> str:
    if not isinstance(value, str):
        return ''
    return value.strip()


def _dedupe(values: list[str]) -> list[str]:
    cleaned: list[str] = []
    for value in values:
        item = _clean_text(value)
        if item and item not in cleaned:
            cleaned.append(item)
    return cleaned


def _message_text(message: Any) -> str:
    text = getattr(message, 'text', None)
    if isinstance(text, str) and text:
        return text
    content = getattr(message, 'content', '')
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return ''.join(
            block.get('text', '')
            for block in content
            if isinstance(block, dict) and block.get('type') == 'text'
        )
    return str(content or '')


def _extract_json_block(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith('```'):
        fence_match = re.match(r'^```(?:json)?\s*(.*?)\s*```$', stripped, flags=re.DOTALL | re.IGNORECASE)
        if fence_match:
            return fence_match.group(1).strip()
    start = stripped.find('{')
    end = stripped.rfind('}')
    if start != -1 and end != -1 and end > start:
        return stripped[start:end + 1].strip()
    return stripped


def _coerce_scope(scope: QueryScope | dict[str, Any] | None) -> QueryScope:
    if isinstance(scope, QueryScope):
        return scope
    return QueryScope.model_validate(scope or {})


def _coerce_memory(memory: ConversationTurn | dict[str, Any] | None) -> ConversationTurn:
    if isinstance(memory, ConversationTurn):
        return memory
    return ConversationTurn.model_validate(memory or {})


class AIQueryRouterError(RuntimeError):
    def __init__(self, message: str, *, raw_output: str = ''):
        super().__init__(message)
        self.raw_output = raw_output


class ConversationTurn(BaseModel):
    """Compact k=1 memory payload for the previous user turn."""

    model_config = ConfigDict(extra='ignore')

    last_user_query: str = ''
    last_standalone_query: str = ''
    last_intent: QueryIntent = QueryIntent.GENERAL_SEMANTIC
    last_intents: list[QueryIntent] = Field(default_factory=list, max_length=5)
    last_entities: list[QueryEntity] = Field(default_factory=list, max_length=20)
    last_scope: QueryScope = Field(default_factory=QueryScope)
    last_answer_summary: str = ''
    last_sources: list[dict[str, str]] = Field(default_factory=list, max_length=20)

    @field_validator('last_user_query', 'last_standalone_query', 'last_answer_summary')
    @classmethod
    def _strip_strings(cls, value: str) -> str:
        return _clean_text(value)

    def has_content(self) -> bool:
        return bool(self.model_dump(mode='json', exclude_defaults=True, exclude_none=True))

    def prompt_payload(self) -> dict[str, Any]:
        return self.model_dump(mode='json', exclude_defaults=True, exclude_none=True)


class AIRouterTask(BaseModel):
    model_config = ConfigDict(extra='ignore')

    route: QueryRoute
    purpose: str
    query: str
    product_codes: list[str] = Field(default_factory=list, max_length=20)
    categories: list[str] = Field(default_factory=list, max_length=10)
    materials: list[str] = Field(default_factory=list, max_length=10)
    exhaustive: bool = False

    @field_validator('purpose', 'query')
    @classmethod
    def _strip_required_strings(cls, value: str) -> str:
        cleaned = _clean_text(value)
        if not cleaned:
            raise ValueError('task fields cannot be empty')
        return cleaned

    @field_validator('product_codes', 'categories', 'materials')
    @classmethod
    def _normalize_lists(cls, values: list[str]) -> list[str]:
        return _dedupe(values)

    def to_query_task(self) -> QueryTask:
        return QueryTask(
            route=self.route,
            purpose=self.purpose,
            query=self.query,
            product_codes=list(self.product_codes),
            categories=list(self.categories),
            materials=list(self.materials),
            exhaustive=self.exhaustive,
        )


class AIRouterPlan(BaseModel):
    model_config = ConfigDict(extra='ignore')

    standalone_query: str
    intent: QueryIntent
    intents: list[QueryIntent] = Field(default_factory=list, max_length=5)
    scope: QueryScope = Field(default_factory=QueryScope)
    entities: list[QueryEntity] = Field(default_factory=list, max_length=20)
    constraints: dict[str, Any] = Field(default_factory=dict)
    subqueries: list[str] = Field(default_factory=list, max_length=5)
    tasks: list[AIRouterTask] = Field(default_factory=list, max_length=5)
    needs_clarification: bool = False
    clarification_question: str = ''
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    memory_used: bool = False

    @field_validator('standalone_query', 'clarification_question')
    @classmethod
    def _strip_strings(cls, value: str) -> str:
        return _clean_text(value)

    @field_validator('subqueries')
    @classmethod
    def _normalize_subqueries(cls, values: list[str]) -> list[str]:
        return _dedupe(values)

    @model_validator(mode='after')
    def _normalize_plan(self) -> 'AIRouterPlan':
        if not self.standalone_query:
            raise ValueError('standalone_query is required')
        if not self.intents:
            self.intents = [self.intent]
        if self.needs_clarification:
            if not self.clarification_question:
                raise ValueError('clarification_question is required when clarification is requested')
            if self.tasks:
                raise ValueError('clarification plans must not include tasks')
        elif not self.tasks:
            raise ValueError('router plans must include at least one task unless clarification is requested')
        return self

    def to_query_plan(self) -> QueryPlan:
        tasks = [task.to_query_task() for task in self.tasks]
        return QueryPlan(
            intent=self.intent,
            intents=list(self.intents) or [self.intent],
            scope=self.scope,
            entities=list(self.entities),
            constraints=dict(self.constraints),
            subqueries=list(self.subqueries) or [task.query for task in tasks],
            tasks=tasks,
            needs_clarification=self.needs_clarification,
            clarification_question=self.clarification_question,
            used_model_planner=True,
        )


class AIRouterResult(BaseModel):
    model_config = ConfigDict(extra='ignore')

    plan: AIRouterPlan
    provider: str
    model: str
    raw_output: str = ''
    repaired_output: str = ''
    repair_attempted: bool = False
    repair_succeeded: bool = False


class AIQueryRouter:
    """Strict AI router with one repair attempt for malformed output."""

    def __init__(
        self,
        *,
        provider: str,
        api_key: str,
        model: str,
        temperature: float = 0.0,
        max_retries: int = 3,
    ):
        provider_name = _clean_text(provider).lower()
        if provider_name not in {'openai', 'gemini'}:
            raise ValueError('provider must be either openai or gemini')
        if not _clean_text(api_key):
            raise ValueError('API key is required for the AI router.')

        self.provider = provider_name
        self.model_name = _clean_text(model)
        self.temperature = temperature
        self.max_retries = max_retries
        self._model = self._build_model(api_key=api_key)

    def _build_model(self, *, api_key: str):
        if self.provider == 'openai':
            return ChatOpenAI(
                model=self.model_name,
                api_key=api_key,
                temperature=self.temperature,
                max_retries=self.max_retries,
            )
        return ChatGoogleGenerativeAI(
            model=self.model_name,
            api_key=api_key,
            temperature=self.temperature,
            max_retries=self.max_retries,
        )

    def _structured_runner(self):
        if not hasattr(self._model, 'with_structured_output'):
            return None
        try:
            return self._model.with_structured_output(
                AIRouterPlan,
                method='json_schema',
                strict=True,
            )
        except TypeError:
            return self._model.with_structured_output(AIRouterPlan)

    def _build_messages(
        self,
        query: str,
        scope: QueryScope,
        memory: ConversationTurn,
    ) -> list[Any]:
        payload = {
            'current_query': _clean_text(query),
            'scope': scope.model_dump(mode='json'),
            'memory': memory.prompt_payload() if memory.has_content() else {},
        }
        user_message = json.dumps(payload, ensure_ascii=True, separators=(',', ':'))
        return [
            SystemMessage(content=AI_ROUTER_SYSTEM_PROMPT),
            HumanMessage(content=user_message),
        ]

    def _parse_plan(self, payload: Any, *, raw_output: str = '') -> AIRouterPlan:
        if isinstance(payload, AIRouterPlan):
            return AIRouterPlan.model_validate(payload.model_dump(mode='json'))
        if isinstance(payload, dict):
            return AIRouterPlan.model_validate(payload)
        if isinstance(payload, str):
            text = payload
        else:
            text = _message_text(payload)
        candidate = _extract_json_block(text)
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise AIQueryRouterError(
                'AI router output was not valid JSON.',
                raw_output=raw_output or text,
            ) from exc
        try:
            return AIRouterPlan.model_validate(parsed)
        except Exception as exc:
            raise AIQueryRouterError(
                'AI router output did not match the required schema.',
                raw_output=raw_output or text,
            ) from exc

    def _primary_attempt(self, messages: list[Any]) -> tuple[AIRouterPlan, str]:
        structured_runner = self._structured_runner()
        if structured_runner is not None:
            payload = structured_runner.invoke(messages)
            return self._parse_plan(payload), ''

        response = self._model.invoke(messages)
        raw_text = _message_text(response)
        return self._parse_plan(raw_text, raw_output=raw_text), raw_text

    def _repair_attempt(self, messages: list[Any], error: Exception) -> tuple[AIRouterPlan, str]:
        raw_output = getattr(error, 'raw_output', '')
        request_payload = _message_text(messages[-1]) if messages else ''
        repair_message = HumanMessage(content=AI_ROUTER_REPAIR_PROMPT.format(
            request=request_payload or '(missing request payload)',
            error=str(error),
            output=raw_output or '(no usable raw output)',
        ))
        response = self._model.invoke([
            SystemMessage(content=AI_ROUTER_SYSTEM_PROMPT),
            repair_message,
        ])
        repaired_text = _message_text(response)
        return self._parse_plan(repaired_text, raw_output=repaired_text), repaired_text

    def plan(
        self,
        query: str,
        *,
        scope: QueryScope | dict[str, Any] | None = None,
        memory: ConversationTurn | dict[str, Any] | None = None,
    ) -> AIRouterResult:
        scope_model = _coerce_scope(scope)
        memory_model = _coerce_memory(memory)
        messages = self._build_messages(_clean_text(query), scope_model, memory_model)

        try:
            plan, raw_output = self._primary_attempt(messages)
            return AIRouterResult(
                plan=plan,
                provider=self.provider,
                model=self.model_name,
                raw_output=raw_output,
                repair_attempted=False,
                repair_succeeded=False,
            )
        except Exception as primary_error:
            try:
                plan, repaired_output = self._repair_attempt(messages, primary_error)
            except Exception as repair_error:
                if isinstance(repair_error, AIQueryRouterError):
                    raise
                raise AIQueryRouterError(
                    'AI router failed to produce a valid plan after one repair attempt.',
                    raw_output=getattr(primary_error, 'raw_output', ''),
                ) from repair_error

            return AIRouterResult(
                plan=plan,
                provider=self.provider,
                model=self.model_name,
                raw_output=getattr(primary_error, 'raw_output', ''),
                repaired_output=repaired_output,
                repair_attempted=True,
                repair_succeeded=True,
            )


__all__ = [
    'AIQueryRouter',
    'AIQueryRouterError',
    'AIRouterPlan',
    'AIRouterResult',
    'AIRouterTask',
    'AI_ROUTER_REPAIR_PROMPT',
    'AI_ROUTER_SYSTEM_PROMPT',
    'ConversationTurn',
]
