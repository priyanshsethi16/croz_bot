"""Conservative English taxonomy matching for extracted product families."""

from __future__ import annotations

import re

from catalog.models import Category


_GREASE_GUN_ACCESSORY_TERMS = (
    'accessory',
    'adaptor',
    'adapter',
    'cartridge',
    'coupler',
    'dispenser',
    'extension',
    'fitting',
    'holder',
    'hose',
    'light',
    'needle',
    'oil gun',
    'swivel',
)


def normalize_taxonomy_text(value: str) -> str:
    value = re.sub(r'[^a-z0-9]+', ' ', str(value or '').lower()).strip()
    words = []
    for word in value.split():
        if len(word) > 3 and word.endswith('s') and not word.endswith('ss'):
            word = word[:-1]
        words.append(word)
    return ' '.join(words)


def _category_terms(category: Category) -> set[str]:
    values = {category.name, category.slug.replace('-', ' '), *(category.aliases or [])}
    return {normalize_taxonomy_text(value) for value in values if normalize_taxonomy_text(value)}


def _is_category_or_descendant(category: Category, root_slug: str) -> bool:
    current = category
    while current is not None:
        if current.slug == root_slug:
            return True
        current = current.parent
    return False


def _is_grease_gun_accessory(product_text: str) -> bool:
    return any(
        re.search(rf'(^|\s){re.escape(term)}($|\s)', product_text)
        for term in _GREASE_GUN_ACCESSORY_TERMS
    )


def resolve_category(
    *,
    product_name: str,
    raw_category: str = '',
    suggestion: str = '',
    categories: list[Category] | None = None,
) -> Category | None:
    """Resolve only exact suggestions/raw labels or clear product-name phrases."""
    categories = categories or list(Category.objects.filter(is_active=True).select_related('parent'))
    suggestion_text = normalize_taxonomy_text(suggestion)
    raw_text = normalize_taxonomy_text(raw_category)
    product_text = normalize_taxonomy_text(product_name)

    # Catalog sub-headings describing an impact-wrench mechanism are not
    # hammer products and must never enter the hammer inventory.
    if 'hammer mechanism' in product_text:
        return None

    term_map: list[tuple[str, Category]] = []
    for category in categories:
        for term in _category_terms(category):
            term_map.append((term, category))

    grease_gun_accessory = _is_grease_gun_accessory(product_text)

    def eligible(category: Category) -> bool:
        return not (
            grease_gun_accessory
            and _is_category_or_descendant(category, 'grease-gun')
        )

    # Suggestions and raw labels must be exact; prefer the more specific match.
    exact_candidates = {suggestion_text, raw_text} - {''}
    exact = [
        category for term, category in term_map
        if term in exact_candidates and eligible(category)
    ]
    if exact:
        return sorted(exact, key=lambda category: len(category.slug), reverse=True)[0]

    # Product names are safer than section banners. Prefer the most specific phrase.
    matches = [
        (term, category)
        for term, category in term_map
        if term
        and eligible(category)
        and re.search(rf'(^|\s){re.escape(term)}($|\s)', product_text)
    ]
    if not matches:
        return None
    matches.sort(key=lambda item: (len(item[0].split()), len(item[0])), reverse=True)
    return matches[0][1]


def descendant_category_ids(category: Category) -> list:
    """Return a category and all descendants without requiring a tree package."""
    found = {category.id}
    frontier = {category.id}
    while frontier:
        children = set(Category.objects.filter(parent_id__in=frontier).values_list('id', flat=True))
        children -= found
        if not children:
            break
        found.update(children)
        frontier = children
    return list(found)
