"""INSERT ... ON CONFLICT helpers that pick the right dialect construct.

SQLAlchemy Core has no portable upsert; the sqlite and postgresql dialects
each ship their own `insert()` with `on_conflict_*`. Rows are chunked so a
big vault flush never trips SQLite's bound-parameter limit.
"""
CHUNK = 500


def _insert_for(conn):
    name = conn.dialect.name
    if name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise RuntimeError(f"Unsupported database dialect: {name}")
    return insert


def insert_ignore(conn, table, rows, keys):
    """Insert rows, silently skipping any whose `keys` already exist."""
    rows = list(rows)
    if not rows:
        return
    insert = _insert_for(conn)
    for i in range(0, len(rows), CHUNK):
        stmt = insert(table).values(rows[i:i + CHUNK]).on_conflict_do_nothing(index_elements=keys)
        conn.execute(stmt)

