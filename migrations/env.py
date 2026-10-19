"""Run the same schema migrations in local development, CI, and production."""

from alembic import context

from api_watch.config import Settings
from api_watch.db import Base, make_database
import api_watch.models  # noqa: F401

config = context.config
settings = Settings()
url = config.attributes.get("database_url", settings.database_url)

if context.is_offline_mode():
    context.configure(url=url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    settings.database_url = url
    engine, _ = make_database(settings)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()
