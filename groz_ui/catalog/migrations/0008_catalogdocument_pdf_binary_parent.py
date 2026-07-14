from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0007_productfamily_aliases'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql="""
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = 'catalog_catalogdocument'
          AND column_name = 'pdf_binary'
    ) THEN
        ALTER TABLE catalog_catalogdocument
            ADD COLUMN pdf_binary bytea NOT NULL DEFAULT '\\x';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = 'catalog_catalogdocument'
          AND column_name = 'parent_id'
    ) THEN
        ALTER TABLE catalog_catalogdocument
            ADD COLUMN parent_id uuid NULL;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.table_constraints
        WHERE table_name = 'catalog_catalogdocument'
          AND constraint_name = 'catalog_catalogdocument_parent_id_fk'
    ) THEN
        ALTER TABLE catalog_catalogdocument
            ADD CONSTRAINT catalog_catalogdocument_parent_id_fk
            FOREIGN KEY (parent_id)
            REFERENCES catalog_catalogdocument (id)
            DEFERRABLE INITIALLY DEFERRED;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS catalog_catalogdocument_parent_id_idx
    ON catalog_catalogdocument (parent_id);
""",
                    reverse_sql="""
DROP INDEX IF EXISTS catalog_catalogdocument_parent_id_idx;
ALTER TABLE catalog_catalogdocument
    DROP CONSTRAINT IF EXISTS catalog_catalogdocument_parent_id_fk;
ALTER TABLE catalog_catalogdocument
    DROP COLUMN IF EXISTS parent_id;
ALTER TABLE catalog_catalogdocument
    DROP COLUMN IF EXISTS pdf_binary;
""",
                ),
            ],
            state_operations=[
                migrations.AddField(
                    model_name='catalogdocument',
                    name='pdf_binary',
                    field=models.BinaryField(blank=True, default=b''),
                ),
                migrations.AddField(
                    model_name='catalogdocument',
                    name='parent',
                    field=models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='split_parts',
                        to='catalog.catalogdocument',
                    ),
                ),
            ],
        ),
    ]
