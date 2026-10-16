"""Private training journals and separate main-menu services."""
from alembic import op
import sqlalchemy as sa

revision = "006_sport_workspace"
down_revision = "005_job_file_comments"
branch_labels = None
depends_on = None


def upgrade():
    from app.database import SportWorkspace

    connection = op.get_bind()
    SportWorkspace.__table__.create(connection, checkfirst=True)
    for slug, name, icon, description in (
        ("sport", "Спорт", "🏋️", "Тренировки, календарь, техника упражнений и история"),
        ("nutrition", "Питание", "🥗", "Дневник питания — раздел готовится"),
    ):
        connection.execute(sa.text('''
            INSERT INTO services(name,slug,route,icon,description,is_active,"order",created_at)
            SELECT :name,:slug,:route,:icon,:description,1,
                   COALESCE((SELECT MAX("order") FROM services),0)+1,CURRENT_TIMESTAMP
            WHERE NOT EXISTS(SELECT 1 FROM services WHERE slug=:slug)
        '''), {"slug": slug, "name": name, "route": "/" + slug,
               "icon": icon, "description": description})


def downgrade():
    raise RuntimeError("Restore a database backup to preserve training history on rollback")
