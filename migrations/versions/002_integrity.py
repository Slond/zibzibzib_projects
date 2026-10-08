"""Session revocation, currency budgets and idempotent transaction creation."""
import secrets

from alembic import op
import sqlalchemy as sa

revision = "002_integrity"
down_revision = "001_finance_events"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    additions = {
        "users": {"session_key": "VARCHAR"},
        "transactions": {
            "event_id": "INTEGER REFERENCES finance_events(id)",
            "currency": "VARCHAR DEFAULT 'KZT'",
            "amount_usd": "NUMERIC(12,2)",
            "exchange_rate": "NUMERIC(12,6)",
            "recurring_id": "INTEGER REFERENCES recurring_transactions(id) ON DELETE SET NULL",
            "scheduled_for": "DATETIME",
        },
        "finance_categories": {"tag_id": "INTEGER REFERENCES category_tags(id)"},
        "category_budgets": {"currency": "VARCHAR NOT NULL DEFAULT 'KZT'"},
        "recurring_transactions": {"anchor_day": "INTEGER"},
        "activity_places": {"corrected_at": "DATETIME"},
    }
    for table, columns in additions.items():
        existing = {column["name"] for column in sa.inspect(connection).get_columns(table)}
        for column, ddl in columns.items():
            if column not in existing:
                op.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {ddl}')
    users = connection.execute(sa.text("SELECT id FROM users WHERE session_key IS NULL OR session_key = ''")).fetchall()
    for (user_id,) in users:
        connection.execute(sa.text("UPDATE users SET session_key=:key WHERE id=:id"), {"key": secrets.token_urlsafe(32), "id": user_id})
    # SQLite must not reuse an identity that still appears in files or old tokens.
    with op.batch_alter_table("users", recreate="always", table_kwargs={"sqlite_autoincrement": True}) as batch:
        batch.alter_column("session_key", existing_type=sa.String(), nullable=False)
    op.execute("UPDATE transactions SET amount_usd=amount, exchange_rate=1 WHERE currency='USD' AND amount_usd IS NULL")
    op.execute("UPDATE recurring_transactions SET anchor_day=CAST(strftime('%d', next_run) AS INTEGER) WHERE anchor_day IS NULL")
    # Only create an index after every column it refers to has been added.
    from app.database import Base
    for table in Base.metadata.tables.values():
        for index in table.indexes:
            index.create(connection, checkfirst=True)


def downgrade():
    raise RuntimeError("Integrity migration requires a database backup for rollback")
