import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

from app.config.settings import settings
from app.db.base import Base
from app.db import models  # noqa: F401
import app.agents.responder_eval.models  # noqa: F401
import app.agents.whatsapp_jit_hold.models  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata
url = os.environ.get("DATABASE_URL_SYNC", settings.database_url_sync)


def run_migrations_offline() -> None:
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(url)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
