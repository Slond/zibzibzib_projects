"""Owner-only comments stored separately from the original CV bytes."""
from alembic import op
import sqlalchemy as sa

revision = "005_job_file_comments"
down_revision = "004_job_search"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    # 004 also creates the current model when initializing a fresh database.
    if "comment" not in {column["name"] for column in sa.inspect(connection).get_columns("job_search_files")}:
        op.add_column("job_search_files", sa.Column("comment", sa.Text(), nullable=False, server_default=""))


def downgrade():
    raise RuntimeError("Restore a database backup to roll back without losing private comments")
