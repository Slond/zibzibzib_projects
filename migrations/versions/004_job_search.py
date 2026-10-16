"""Private job search workspaces, original files and revocable sharing."""
from alembic import op
import sqlalchemy as sa

revision = "004_job_search"
down_revision = "003_activity_pipeline"
branch_labels = None
depends_on = None


def upgrade():
    from app.database import JobSearchWorkspace, JobSearchFile

    connection = op.get_bind()
    JobSearchWorkspace.__table__.create(connection, checkfirst=True)
    JobSearchFile.__table__.create(connection, checkfirst=True)
    connection.execute(sa.text('''
        INSERT INTO services(name,slug,route,icon,description,is_active,"order",created_at)
        SELECT :name,'jobs','/jobs','💼',:description,1,
               COALESCE((SELECT MAX("order") FROM services),0)+1,CURRENT_TIMESTAMP
        WHERE NOT EXISTS(SELECT 1 FROM services WHERE slug='jobs')
    '''), {"name": "Поиск работы", "description": "Отклики, собеседования, документы и воронка поиска"})


def downgrade():
    raise RuntimeError("Restore a database backup to roll back job search without losing files")
