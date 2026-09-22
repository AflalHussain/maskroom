"""Engine creation, URL resolution and schema bootstrap.

Resolution order for the database:
  1. MASKROOM_DATABASE_URL (any SQLAlchemy URL; `postgres://` is normalised to
     the psycopg driver).
  2. Otherwise SQLite at <MASKROOM_DATA_DIR>/maskroom.db, or, with no data dir
     set, <repo>/webui/runs/maskroom.db (the same scratch tree the web UI has
     always used, git-ignored).

connect() caches one Engine per URL per process and creates the schema the
first time, so the web app, the admin CLI and any test share a single engine
for the same URL. `sqlite://` (in-memory) uses a StaticPool so every thread
sees the same database; it is meant for tests only.
"""
import os
import threading
import time

from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.pool import StaticPool

from ._upsert import insert_ignore
from .schema import metadata, schema_meta

SCHEMA_VERSION = 1
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_MEMORY_URLS = ("sqlite://", "sqlite:///:memory:")


def data_dir():
    """MASKROOM_DATA_DIR if set, else None (callers fall back to legacy paths)."""
    return os.environ.get("MASKROOM_DATA_DIR") or None


def default_sqlite_path():
    base = data_dir() or os.path.join(_REPO_ROOT, "webui", "runs")
    return os.path.join(base, "maskroom.db")


def normalize_url(url):
    """Map `postgres://` / bare `postgresql://` onto the psycopg 3 driver."""
    if not url:
        return url
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def default_url():
    url = os.environ.get("MASKROOM_DATABASE_URL")
    return normalize_url(url) if url else "sqlite:///" + default_sqlite_path()


def is_configured():
    """Whether opening the default database would find existing state: a URL
    is set, or the default SQLite file already exists. Library callers (the
    CLI engine loading the overlay) use this to avoid creating a database as
    a side effect of a one-off run."""
    return bool(os.environ.get("MASKROOM_DATABASE_URL")) or os.path.isfile(default_sqlite_path())


def make_engine(url):
    url = normalize_url(url)
    if url.startswith("sqlite"):
        memory = url in _MEMORY_URLS
        kw = {"connect_args": {"check_same_thread": False}}
        if memory:
            kw["poolclass"] = StaticPool
        else:
            path = make_url(url).database
            if path:
                os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        engine = create_engine(url, **kw)

        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            if not memory:
                cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()
        return engine
    # Postgres: gunicorn runs 1 worker x 4 threads plus the occasional sweep.
    return create_engine(url, pool_size=5, max_overflow=5, pool_pre_ping=True,
                         pool_recycle=1800)


def init_schema(engine):
    metadata.create_all(engine)
    with engine.begin() as conn:
        insert_ignore(conn, schema_meta,
                      [{"version": SCHEMA_VERSION, "applied_at": time.time()}], keys=["version"])


_engines = {}
_lock = threading.Lock()


def connect(url=None):
    """The process-wide Engine for `url` (default: default_url()), with the
    schema created on first use."""
    url = normalize_url(url) or default_url()
    with _lock:
        engine = _engines.get(url)
        if engine is None:
            engine = make_engine(url)
            init_schema(engine)
            _engines[url] = engine
        return engine


def dispose_all():
    """Drop every cached engine (tests)."""
    with _lock:
        for engine in _engines.values():
            engine.dispose()
        _engines.clear()
