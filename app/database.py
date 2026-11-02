import os
import threading
from contextvars import ContextVar

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args)

# Every person who signs in has their own data: their own database (and folders, see tenant_path). `engine` is the
# first person's (what existed before sign-in) and also holds the `users` table. Which database a session opens is
# decided by the user the current request or job runs for, so no query needs to know about users.
current_user: ContextVar[int | None] = ContextVar("current_user", default=None)
FIRST_USER = 1

_factories: dict[int, sessionmaker] = {}
_factories_lock = threading.Lock()


def _own_path(user_id: int, base: str) -> str:
    """<data dir>/tenants/<id>/<name> for the folder or file `base` (./data/pdfs → ./data/tenants/2/pdfs)."""
    base = os.path.normpath(base)
    return os.path.join(os.path.dirname(base), "tenants", str(user_id), os.path.basename(base))


def tenant_path(base: str) -> str:
    """`base` for the first user; the same folder under data/tenants/<id>/ for everyone else."""
    user_id = current_user.get()
    return base if user_id in (None, FIRST_USER) else _own_path(user_id, base)


def _factory_for(user_id: int | None) -> sessionmaker:
    if user_id in (None, FIRST_USER):
        return _main_factory
    with _factories_lock:
        if user_id not in _factories:
            if not settings.database_url.startswith("sqlite"):
                raise RuntimeError("A separate database per person is only set up for SQLite (DATABASE_URL=sqlite:///…)")
            path = os.path.join(
                os.path.dirname(os.path.normpath(engine.url.database)), "tenants", str(user_id), "bot.db"
            )
            os.makedirs(os.path.dirname(path), exist_ok=True)
            own = create_engine(f"sqlite:///{path}", connect_args=connect_args)
            Base.metadata.create_all(bind=own)
            _add_missing_columns(own)
            _factories[user_id] = sessionmaker(autocommit=False, autoflush=False, bind=own)
        return _factories[user_id]


_main_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class _TenantSessions:
    """SessionLocal(): a session on the database of the user the caller is working for."""

    def __call__(self, **kwargs):
        return _factory_for(current_user.get())(**kwargs)


SessionLocal = _TenantSessions()


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from app import models  # noqa: F401  (register models on Base.metadata)

    Base.metadata.create_all(bind=engine)
    _add_missing_columns(engine)


def _add_missing_columns(engine) -> None:
    """create_all() only creates missing TABLES; a column added to a model
    later never reaches an existing database. Add those (as nullable
    columns) so upgrading doesn't require deleting the db. Not a general
    migration tool — renames/type changes still need a manual migration."""
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name not in existing:
                    col_type = column.type.compile(dialect=engine.dialect)
                    conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}'))
                    if (table.name, column.name) == ("processed_emails", "seen_at"):
                        # Emails from before this column existed are history, not news.
                        conn.execute(text("UPDATE processed_emails SET seen_at = processed_at"))
