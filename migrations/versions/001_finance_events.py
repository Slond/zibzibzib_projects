"""Add finance events system

Revision ID: 001_finance_events
Revises: 
Create Date: 2026-03-05

Adds:
- finance_events table
- finance_event_members table
- category_tags table
- category_budgets table
- recurring_transactions table
- exchange_rates table
- New columns to transactions: event_id, currency, amount_usd, exchange_rate
- New column to finance_categories: tag_id
- Creates default event for existing accounts and migrates transactions
"""
from typing import Sequence, Union
import secrets
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


revision: str = "001_finance_events"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def generate_webhook_token():
    return secrets.token_urlsafe(32)


def upgrade() -> None:
    conn = op.get_bind()
    
    # 1. Create finance_events table
    op.create_table(
        "finance_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("finance_accounts.id"), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(), server_default="personal"),
        sa.Column("default_currency", sa.String(), server_default="KZT"),
        sa.Column("webhook_token", sa.String(), unique=True, nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="1"),
        sa.Column("is_archived", sa.Boolean(), server_default="0"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp()),
    )
    op.create_index("idx_finance_event_account", "finance_events", ["account_id"])
    op.create_index("idx_finance_event_webhook", "finance_events", ["webhook_token"])
    
    # 2. Create finance_event_members table
    op.create_table(
        "finance_event_members",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("finance_events.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("role", sa.String(), server_default="member"),
        sa.Column("joined_at", sa.DateTime(), server_default=sa.func.current_timestamp()),
    )
    op.create_index("idx_event_member_event", "finance_event_members", ["event_id"])
    op.create_index("idx_event_member_user", "finance_event_members", ["user_id"])
    op.create_index("idx_event_member_unique", "finance_event_members", ["event_id", "user_id"], unique=True)
    
    # 3. Create category_tags table
    op.create_table(
        "category_tags",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("finance_accounts.id"), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("color", sa.String(), nullable=True),
        sa.Column("display_order", sa.Integer(), server_default="0"),
    )
    op.create_index("idx_category_tag_account", "category_tags", ["account_id"])
    op.create_index("idx_category_tag_account_name", "category_tags", ["account_id", "name"], unique=True)
    
    # 4. Create category_budgets table
    op.create_table(
        "category_budgets",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("category_id", sa.Integer(), sa.ForeignKey("finance_categories.id"), nullable=False),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("finance_events.id"), nullable=True),
        sa.Column("monthly_limit", sa.Numeric(12, 2), nullable=False),
        sa.Column("alert_threshold", sa.Integer(), server_default="70"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp()),
    )
    op.create_index("idx_budget_category", "category_budgets", ["category_id"])
    op.create_index("idx_budget_event", "category_budgets", ["event_id"])
    op.create_index("idx_budget_category_event", "category_budgets", ["category_id", "event_id"], unique=True)
    
    # 5. Create recurring_transactions table
    op.create_table(
        "recurring_transactions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("finance_events.id"), nullable=False),
        sa.Column("category_id", sa.Integer(), sa.ForeignKey("finance_categories.id"), nullable=True),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(), server_default="KZT"),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("frequency", sa.String(), nullable=False),
        sa.Column("frequency_value", sa.Integer(), server_default="1"),
        sa.Column("next_run", sa.DateTime(), nullable=False),
        sa.Column("last_run", sa.DateTime(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="1"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp()),
    )
    op.create_index("idx_recurring_event", "recurring_transactions", ["event_id"])
    op.create_index("idx_recurring_category", "recurring_transactions", ["category_id"])
    
    # 6. Create exchange_rates table
    op.create_table(
        "exchange_rates",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("from_currency", sa.String(), nullable=False),
        sa.Column("to_currency", sa.String(), nullable=False),
        sa.Column("rate", sa.Numeric(12, 6), nullable=False),
        sa.Column("rate_date", sa.Date(), nullable=False),
    )
    op.create_index("idx_exchange_rate_date", "exchange_rates", ["rate_date"])
    op.create_index("idx_exchange_rate_currencies_date", "exchange_rates", 
                   ["from_currency", "to_currency", "rate_date"], unique=True)
    
    # 7. Add new columns to transactions table
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.add_column(sa.Column("event_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("currency", sa.String(), server_default="KZT"))
        batch_op.add_column(sa.Column("amount_usd", sa.Numeric(12, 2), nullable=True))
        batch_op.add_column(sa.Column("exchange_rate", sa.Numeric(12, 6), nullable=True))
        batch_op.create_foreign_key("fk_transaction_event", "finance_events", ["event_id"], ["id"])
    
    op.create_index("idx_transaction_event", "transactions", ["event_id"])
    op.create_index("idx_transaction_event_timestamp", "transactions", ["event_id", "timestamp"])
    
    # 8. Add tag_id to finance_categories
    with op.batch_alter_table("finance_categories") as batch_op:
        batch_op.add_column(sa.Column("tag_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key("fk_category_tag", "category_tags", ["tag_id"], ["id"])
    
    op.create_index("idx_category_tag", "finance_categories", ["tag_id"])
    
    # 9. Create default events for existing accounts and migrate transactions
    now = datetime.now(tz=timezone.utc).isoformat()
    
    accounts = conn.execute(text("SELECT id, name FROM finance_accounts")).fetchall()
    
    for account_id, account_name in accounts:
        existing = conn.execute(
            text("SELECT id FROM finance_events WHERE account_id = :aid"),
            {"aid": account_id}
        ).fetchone()
        
        if not existing:
            webhook_token = generate_webhook_token()
            
            conn.execute(
                text("""
                    INSERT INTO finance_events (account_id, name, event_type, default_currency, webhook_token, is_active, is_archived, created_at)
                    VALUES (:account_id, 'Ежедневные траты', 'personal', 'KZT', :webhook_token, 1, 0, :created_at)
                """),
                {"account_id": account_id, "webhook_token": webhook_token, "created_at": now}
            )
            
            event_id = conn.execute(text("SELECT last_insert_rowid()")).scalar()
            
            users = conn.execute(
                text("SELECT id FROM users WHERE finance_account_id = :aid"),
                {"aid": account_id}
            ).fetchall()
            
            for (user_id,) in users:
                conn.execute(
                    text("""
                        INSERT INTO finance_event_members (event_id, user_id, role, joined_at)
                        VALUES (:event_id, :user_id, 'owner', :joined_at)
                    """),
                    {"event_id": event_id, "user_id": user_id, "joined_at": now}
                )
            
            conn.execute(
                text("""
                    UPDATE transactions 
                    SET event_id = :event_id, currency = 'KZT'
                    WHERE account_id = :account_id AND event_id IS NULL
                """),
                {"event_id": event_id, "account_id": account_id}
            )


def downgrade() -> None:
    # Remove indexes
    op.drop_index("idx_category_tag", table_name="finance_categories")
    op.drop_index("idx_transaction_event_timestamp", table_name="transactions")
    op.drop_index("idx_transaction_event", table_name="transactions")
    
    # Remove columns from finance_categories
    with op.batch_alter_table("finance_categories") as batch_op:
        batch_op.drop_constraint("fk_category_tag", type_="foreignkey")
        batch_op.drop_column("tag_id")
    
    # Remove columns from transactions
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.drop_constraint("fk_transaction_event", type_="foreignkey")
        batch_op.drop_column("exchange_rate")
        batch_op.drop_column("amount_usd")
        batch_op.drop_column("currency")
        batch_op.drop_column("event_id")
    
    # Drop tables
    op.drop_table("exchange_rates")
    op.drop_table("recurring_transactions")
    op.drop_table("category_budgets")
    op.drop_table("category_tags")
    op.drop_table("finance_event_members")
    op.drop_table("finance_events")
