import re
import uuid

from django.db import migrations


NAMESPACE = uuid.UUID('9a56bd37-b8ee-4ab0-9263-e52dc80cd3ef')

CATEGORIES = (
    ('grease-gun', 'Grease Gun', None, ['grease guns']),
    ('lever-grease-gun', 'Lever Grease Gun', 'grease-gun', ['lever grease guns']),
    (
        'pistol-grip-grease-gun',
        'Pistol Grip Grease Gun',
        'grease-gun',
        ['pistol grip grease guns', 'pistol grease gun', 'pistol grease guns'],
    ),
    (
        'air-operated-grease-gun',
        'Air Operated Grease Gun',
        'grease-gun',
        ['air operated grease guns'],
    ),
    (
        'battery-operated-grease-gun',
        'Battery Operated Grease Gun',
        'grease-gun',
        [
            'battery operated grease guns',
            'battery powered grease gun',
            'battery powered grease guns',
            'cordless grease gun',
            'cordless grease guns',
        ],
    ),
    ('mini-grease-gun', 'Mini Grease Gun', 'grease-gun', ['mini grease guns']),
)

ACCESSORY_TERMS = (
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


def _normalize(value):
    value = re.sub(r'[^a-z0-9]+', ' ', str(value or '').lower()).strip()
    words = []
    for word in value.split():
        if len(word) > 3 and word.endswith('s') and not word.endswith('ss'):
            word = word[:-1]
        words.append(word)
    return ' '.join(words)


def _contains_phrase(text, phrase):
    return bool(re.search(rf'(^|\s){re.escape(phrase)}($|\s)', text))


def seed_and_assign_categories(apps, schema_editor):
    Category = apps.get_model('catalog', 'Category')
    ProductFamily = apps.get_model('catalog', 'ProductFamily')

    created = {}
    for slug, name, parent_slug, aliases in CATEGORIES:
        category, _ = Category.objects.update_or_create(
            slug=slug,
            defaults={
                'id': uuid.uuid5(NAMESPACE, slug),
                'name': name,
                'aliases': aliases,
                'is_active': True,
            },
        )
        created[slug] = category

    for slug, _, parent_slug, _ in CATEGORIES:
        if parent_slug:
            Category.objects.filter(slug=slug).update(parent=created[parent_slug])

    term_map = []
    for slug, name, _, aliases in CATEGORIES:
        for value in (name, slug.replace('-', ' '), *aliases):
            term_map.append((_normalize(value), created[slug]))

    families = list(ProductFamily.objects.filter(normalized_category__isnull=True))
    changed = []
    for family in families:
        product_text = _normalize(family.product_name)
        if any(_contains_phrase(product_text, term) for term in ACCESSORY_TERMS):
            continue

        raw_text = _normalize(family.raw_category)
        raw_extraction = family.raw_extraction if isinstance(family.raw_extraction, dict) else {}
        suggestion_text = _normalize(raw_extraction.get('normalized_category_suggestion'))
        exact_candidates = {suggestion_text, raw_text} - {''}
        matches = [
            category for term, category in term_map
            if term in exact_candidates
        ]
        if not matches:
            matches = [
                category for term, category in term_map
                if term and _contains_phrase(product_text, term)
            ]
        if not matches:
            continue
        matches.sort(key=lambda category: len(category.slug), reverse=True)
        family.normalized_category = matches[0]
        changed.append(family)

    if changed:
        ProductFamily.objects.bulk_update(changed, ('normalized_category',), batch_size=250)


def unseed_categories(apps, schema_editor):
    Category = apps.get_model('catalog', 'Category')
    ProductFamily = apps.get_model('catalog', 'ProductFamily')
    slugs = [row[0] for row in CATEGORIES]
    category_ids = list(Category.objects.filter(slug__in=slugs).values_list('id', flat=True))
    ProductFamily.objects.filter(normalized_category_id__in=category_ids).update(normalized_category=None)
    Category.objects.filter(slug__in=slugs).update(parent=None)
    Category.objects.filter(slug__in=slugs).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('catalog', '0008_catalogdocument_pdf_binary_parent'),
    ]

    operations = [
        migrations.RunPython(seed_and_assign_categories, unseed_categories),
    ]
