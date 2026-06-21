"""Conservative English taxonomy matching for extracted product families."""

from __future__ import annotations

import re

from catalog.models import Category


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

    # A model suggestion/raw label is accepted only as an exact reviewed term.
    for candidate in (suggestion_text, raw_text):
        if candidate:
            exact = [category for term, category in term_map if term == candidate]
            if exact:
                return sorted(exact, key=lambda category: len(category.slug), reverse=True)[0]

    # Product names are safer than section banners. Prefer the most specific phrase.
    matches = [
        (term, category)
        for term, category in term_map
        if term and re.search(rf'(^|\s){re.escape(term)}($|\s)', product_text)
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
