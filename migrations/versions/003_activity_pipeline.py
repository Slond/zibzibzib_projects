"""Durable activity events, deduplication and revisioned day jobs."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from alembic import op
import sqlalchemy as sa

revision = "003_activity_pipeline"
down_revision = "002_integrity"
branch_labels = None
depends_on = None


def upgrade():
    con = op.get_bind()
    columns = {c["name"] for c in sa.inspect(con).get_columns("activity_events")}
    for name, ddl in {
        "client_event_id": "VARCHAR(80)",
        "idle_seconds": "FLOAT",
        "sample_minutes": "INTEGER",
        "actor": "VARCHAR(12)",
    }.items():
        if name not in columns:
            op.execute(f"ALTER TABLE activity_events ADD COLUMN {name} {ddl}")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_activity_device_event ON activity_events(device_id,client_event_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_activity_device_kind_time ON activity_events(device_id,kind,recorded_at)"
    )
    if "labels_json" not in {
        c["name"] for c in sa.inspect(con).get_columns("activity_places")
    }:
        op.execute("ALTER TABLE activity_places ADD COLUMN labels_json TEXT")
    if "client_version" not in {
        c["name"] for c in sa.inspect(con).get_columns("activity_devices")
    }:
        op.execute("ALTER TABLE activity_devices ADD COLUMN client_version VARCHAR(20)")
    from app.database import ActivityDay

    ActivityDay.__table__.create(con, checkfirst=True)
    # The old value covered only screenshots, not the event history. Preserve history by default.
    op.execute("UPDATE activity_settings SET retention_days=0")
    settings = dict(
        con.execute(
            sa.text("SELECT user_id,timezone FROM activity_settings")
        ).fetchall()
    )
    days = set()
    for uid, at, kind, interval in con.execute(
        sa.text(
            "SELECT DISTINCT user_id,recorded_at,kind,sample_minutes FROM activity_events"
        )
    ):
        try:
            tz = ZoneInfo(settings.get(uid) or "Asia/Almaty")
        except Exception:
            tz = ZoneInfo("Asia/Almaty")
        stamp = datetime.fromisoformat(str(at)).replace(tzinfo=timezone.utc)
        days.add((uid, stamp.astimezone(tz).date().isoformat()))
        days.add(
            (
                uid,
                (
                    stamp
                    + timedelta(minutes=45 if kind == "location" else (interval or 1))
                )
                .astimezone(tz)
                .date()
                .isoformat(),
            )
        )
    for uid, day in days:
        con.execute(
            sa.text(
                "INSERT OR IGNORE INTO activity_days(user_id,day,revision,processed_revision,attempts,next_run_at) VALUES(:uid,:day,1,0,0,CURRENT_TIMESTAMP)"
            ),
            {"uid": uid, "day": day},
        )


def downgrade():
    raise RuntimeError("Restore a database backup to roll back the activity pipeline")
