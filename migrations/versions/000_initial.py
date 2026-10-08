"""Baseline schema for fresh databases and existing unversioned installations."""
from alembic import op

revision = "000_initial"
down_revision = None
branch_labels = None
depends_on = None

STATEMENTS = [
    """
CREATE TABLE IF NOT EXISTS devices (
	id INTEGER NOT NULL, 
	device_id VARCHAR NOT NULL, 
	name VARCHAR NOT NULL, 
	device_type VARCHAR, 
	room VARCHAR, 
	updated_at DATETIME, 
	PRIMARY KEY (id)
)

""",
    """
CREATE TABLE IF NOT EXISTS exchange_rates (
	id INTEGER NOT NULL, 
	from_currency VARCHAR NOT NULL, 
	to_currency VARCHAR NOT NULL, 
	rate NUMERIC(12, 6) NOT NULL, 
	rate_date DATE NOT NULL, 
	PRIMARY KEY (id)
)

""",
    """
CREATE TABLE IF NOT EXISTS finance_accounts (
	id INTEGER NOT NULL, 
	name VARCHAR NOT NULL, 
	created_at DATETIME, 
	PRIMARY KEY (id)
)

""",
    """
CREATE TABLE IF NOT EXISTS iqair_sensors (
	id INTEGER NOT NULL, 
	url VARCHAR NOT NULL, 
	name VARCHAR NOT NULL, 
	is_active BOOLEAN, 
	track_pm25 BOOLEAN, 
	track_pm10 BOOLEAN, 
	created_at DATETIME, 
	last_poll DATETIME, 
	PRIMARY KEY (id), 
	UNIQUE (url)
)

""",
    """
CREATE TABLE IF NOT EXISTS measurements (
	id INTEGER NOT NULL, 
	device_id VARCHAR NOT NULL, 
	property_instance VARCHAR NOT NULL, 
	value FLOAT NOT NULL, 
	unit VARCHAR, 
	timestamp DATETIME, 
	PRIMARY KEY (id)
)

""",
    """
CREATE TABLE IF NOT EXISTS services (
	id INTEGER NOT NULL, 
	name VARCHAR NOT NULL, 
	slug VARCHAR NOT NULL, 
	route VARCHAR NOT NULL, 
	icon VARCHAR, 
	description VARCHAR, 
	is_active BOOLEAN, 
	"order" INTEGER, 
	created_at DATETIME, 
	PRIMARY KEY (id)
)

""",
    """
CREATE TABLE IF NOT EXISTS weather_display_settings (
	id INTEGER NOT NULL, 
	device_id VARCHAR NOT NULL, 
	property_instance VARCHAR NOT NULL, 
	show_on_dashboard BOOLEAN, 
	display_order INTEGER, 
	PRIMARY KEY (id)
)

""",
    """
CREATE TABLE IF NOT EXISTS category_tags (
	id INTEGER NOT NULL, 
	account_id INTEGER NOT NULL, 
	name VARCHAR NOT NULL, 
	color VARCHAR, 
	display_order INTEGER, 
	PRIMARY KEY (id), 
	FOREIGN KEY(account_id) REFERENCES finance_accounts (id)
)

""",
    """
CREATE TABLE IF NOT EXISTS finance_events (
	id INTEGER NOT NULL, 
	account_id INTEGER NOT NULL, 
	name VARCHAR NOT NULL, 
	event_type VARCHAR, 
	default_currency VARCHAR, 
	webhook_token VARCHAR NOT NULL, 
	is_active BOOLEAN, 
	is_archived BOOLEAN, 
	created_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(account_id) REFERENCES finance_accounts (id), 
	UNIQUE (webhook_token)
)

""",
    """
CREATE TABLE IF NOT EXISTS users (
	id INTEGER NOT NULL, 
	email VARCHAR NOT NULL, 
	password_hash VARCHAR NOT NULL, 
	name VARCHAR, 
	is_admin BOOLEAN, 
	must_change_password BOOLEAN, 
	finance_account_id INTEGER, 
	created_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(finance_account_id) REFERENCES finance_accounts (id)
)

""",
    """
CREATE TABLE IF NOT EXISTS activity_devices (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	name VARCHAR NOT NULL, 
	platform VARCHAR NOT NULL, 
	token VARCHAR NOT NULL, 
	created_at DATETIME, 
	last_seen_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id)
)

""",
    """
CREATE TABLE IF NOT EXISTS activity_places (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	name VARCHAR NOT NULL, 
	note VARCHAR, 
	wifi_ssid VARCHAR, 
	latitude FLOAT, 
	longitude FLOAT, 
	radius_m INTEGER, 
	created_at DATETIME, 
	corrected_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id)
)

""",
    """
CREATE TABLE IF NOT EXISTS activity_settings (
	user_id INTEGER NOT NULL, 
	openai_api_key VARCHAR, 
	openai_model VARCHAR, 
	retention_days INTEGER, 
	timezone VARCHAR, 
	sample_minutes INTEGER, 
	idle_minutes INTEGER, 
	skip_apps TEXT, 
	PRIMARY KEY (user_id), 
	FOREIGN KEY(user_id) REFERENCES users (id)
)

""",
    """
CREATE TABLE IF NOT EXISTS finance_categories (
	id INTEGER NOT NULL, 
	account_id INTEGER NOT NULL, 
	name VARCHAR NOT NULL, 
	icon VARCHAR, 
	usage_count INTEGER, 
	tag_id INTEGER, 
	PRIMARY KEY (id), 
	FOREIGN KEY(account_id) REFERENCES finance_accounts (id), 
	FOREIGN KEY(tag_id) REFERENCES category_tags (id)
)

""",
    """
CREATE TABLE IF NOT EXISTS finance_event_members (
	id INTEGER NOT NULL, 
	event_id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	role VARCHAR, 
	joined_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(event_id) REFERENCES finance_events (id), 
	FOREIGN KEY(user_id) REFERENCES users (id)
)

""",
    """
CREATE TABLE IF NOT EXISTS transactions (
	id INTEGER NOT NULL, 
	account_id INTEGER NOT NULL, 
	event_id INTEGER, 
	amount NUMERIC(12, 2) NOT NULL, 
	currency VARCHAR, 
	amount_usd NUMERIC(12, 2), 
	exchange_rate NUMERIC(12, 6), 
	category VARCHAR, 
	description VARCHAR, 
	timestamp DATETIME, 
	source VARCHAR, 
	created_by_user_id INTEGER, 
	PRIMARY KEY (id), 
	FOREIGN KEY(account_id) REFERENCES finance_accounts (id), 
	FOREIGN KEY(event_id) REFERENCES finance_events (id), 
	FOREIGN KEY(created_by_user_id) REFERENCES users (id)
)

""",
    """
CREATE TABLE IF NOT EXISTS user_service_access (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	service_id INTEGER NOT NULL, 
	role VARCHAR, 
	granted_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id), 
	FOREIGN KEY(service_id) REFERENCES services (id)
)

""",
    """
CREATE TABLE IF NOT EXISTS activity_events (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	device_id INTEGER NOT NULL, 
	place_id INTEGER, 
	recorded_at DATETIME, 
	kind VARCHAR NOT NULL, 
	app_name VARCHAR, 
	window_title VARCHAR, 
	wifi_ssid VARCHAR, 
	latitude FLOAT, 
	longitude FLOAT, 
	accuracy_m FLOAT, 
	screenshot_path VARCHAR, 
	summary TEXT, 
	analysis_status VARCHAR, 
	analysis_error TEXT, 
	created_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id), 
	FOREIGN KEY(device_id) REFERENCES activity_devices (id), 
	FOREIGN KEY(place_id) REFERENCES activity_places (id)
)

""",
    """
CREATE TABLE IF NOT EXISTS category_budgets (
	id INTEGER NOT NULL, 
	category_id INTEGER NOT NULL, 
	event_id INTEGER, 
	monthly_limit NUMERIC(12, 2) NOT NULL, 
	alert_threshold INTEGER, 
	created_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(category_id) REFERENCES finance_categories (id), 
	FOREIGN KEY(event_id) REFERENCES finance_events (id)
)

""",
    """
CREATE TABLE IF NOT EXISTS recurring_transactions (
	id INTEGER NOT NULL, 
	event_id INTEGER NOT NULL, 
	category_id INTEGER, 
	amount NUMERIC(12, 2) NOT NULL, 
	currency VARCHAR, 
	description VARCHAR, 
	frequency VARCHAR NOT NULL, 
	frequency_value INTEGER, 
	next_run DATETIME NOT NULL, 
	last_run DATETIME, 
	is_active BOOLEAN, 
	created_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(event_id) REFERENCES finance_events (id), 
	FOREIGN KEY(category_id) REFERENCES finance_categories (id)
)

""",
]

def upgrade():
    for statement in STATEMENTS:
        op.execute(statement)

def downgrade():
    raise RuntimeError("Baseline downgrade would destroy application data")
