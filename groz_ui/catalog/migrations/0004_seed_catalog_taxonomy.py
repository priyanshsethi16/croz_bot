import uuid

from django.db import migrations


NAMESPACE = uuid.UUID('9a56bd37-b8ee-4ab0-9263-e52dc80cd3ef')

CATEGORIES = (
    ('hammer', 'Hammer', None, ['hammers', 'striking tool', 'striking tools']),
    ('club-hammer', 'Club Hammer', 'hammer', ['club hammers']),
    ('sledge-hammer', 'Sledge Hammer', 'hammer', ['sledge hammers']),
    ('copper-hammer', 'Copper Hammer', 'hammer', ['copper hammers', 'non-sparking hammer']),
    ('claw-hammer', 'Claw Hammer', 'hammer', ['claw hammers']),
    ('ball-pein-hammer', 'Ball Pein Hammer', 'hammer', ['ball peen hammer', 'ball pein hammers']),
    ('mallet', 'Mallet', 'hammer', ['mallets', 'rubber mallet']),
    ('pliers', 'Pliers', None, ['plier']),
    ('water-pump-pliers', 'Water Pump Pliers', 'pliers', ['waterpump pliers', 'tongue and groove pliers']),
    ('wrench', 'Wrench', None, ['wrenches']),
    ('impact-wrench', 'Impact Wrench', 'wrench', ['impact wrenches']),
    ('torque-wrench', 'Torque Wrench', 'wrench', ['torque wrenches']),
    ('screwdriver', 'Screwdriver', None, ['screwdrivers']),
    ('vde-screwdriver', 'VDE Screwdriver', 'screwdriver', ['insulated screwdriver', '1000v screwdriver']),
)


def seed_categories(apps, schema_editor):
    Category = apps.get_model('catalog', 'Category')
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


def unseed_categories(apps, schema_editor):
    Category = apps.get_model('catalog', 'Category')
    queryset = Category.objects.filter(slug__in=[row[0] for row in CATEGORIES])
    queryset.update(parent=None)
    queryset.delete()


class Migration(migrations.Migration):
    dependencies = [
        ('catalog', '0003_catalog_rag_v2_schema'),
    ]

    operations = [
        migrations.RunPython(seed_categories, unseed_categories),
    ]
