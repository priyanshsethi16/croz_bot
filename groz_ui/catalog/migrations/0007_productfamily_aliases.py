from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0006_document_source_schema'),
    ]

    operations = [
        migrations.AddField(
            model_name='productfamily',
            name='aliases',
            field=models.JSONField(blank=True, default=list),
        ),
    ]
