"""
db.py — Database Abstraction Layer
Supports: SQLite (default) | PostgreSQL (via DATABASE_URL env var)

Usage:
  SQLite  : DATABASE_URL not set  → uses data.db
  Postgres: DATABASE_URL=postgresql://user:pass@host:5432/dbname

All SQL should use ON CONFLICT syntax (supported by both), not INSERT OR REPLACE.
Placeholders: always write ? — adapter converts to %s for Postgres.
"""

import os
import re
import threading
import logging
import sqlite3
from contextlib import contextmanager

log = logging.getLogger("db")

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_POSTGRES  = bool(DATABASE_URL)
_DB_PATH      = os.environ.get("SQLITE_PATH", "data.db")

# ─── Universal Row ─────────────────────────────────────────────────────────────
class UniversalRow:
    """
    Wraps a DB row so code can use:
      row[0]          (index access)
      row["col"]      (dict access)
      row.col         (attribute access)
      dict(row)       (conversion)
    Works identically for both SQLite and PostgreSQL results.
    """
    __slots__ = ("_data", "_keys")

    def __init__(self, keys, values):
        self._keys = list(keys)
        self._data = dict(zip(keys, values))

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._data[self._keys[key]]
        return self._data[key]

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self._data[name]
        except KeyError:
            raise AttributeError(name)

    def __iter__(self):
        return iter(self._data.values())

    def keys(self):
        return self._keys

    def get(self, key, default=None):
        return self._data.get(key, default)

    def __repr__(self):
        return f"UniversalRow({self._data})"

    def _asdict(self):
        return dict(self._data)


# ─── SQL Translation ───────────────────────────────────────────────────────────
_PLACEHOLDER_RE = re.compile(r"\?")
_INSERT_OR_IGNORE_RE = re.compile(
    r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", re.IGNORECASE
)
_INSERT_OR_REPLACE_RE = re.compile(
    r"\bINSERT\s+OR\s+REPLACE\s+INTO\b", re.IGNORECASE
)
# SQLite date math → Postgres
_DATE_NOW_PKT_RE = re.compile(
    r"DATE\('now',\s*'\+5 hours'\)", re.IGNORECASE
)
_DATE_NOW_RE = re.compile(r"DATE\('now'[^)]*\)", re.IGNORECASE)

def _to_pg(sql: str) -> str:
    """Convert SQLite-flavored SQL to PostgreSQL dialect."""
    sql = _PLACEHOLDER_RE.sub("%s", sql)
    sql = _INSERT_OR_IGNORE_RE.sub("INSERT INTO", sql)
    # INSERT OR REPLACE → requires ON CONFLICT clause (already present in our new code)
    sql = _INSERT_OR_REPLACE_RE.sub("INSERT INTO", sql)
    # Date math
    sql = _DATE_NOW_PKT_RE.sub(
        "(NOW() AT TIME ZONE 'Asia/Karachi')::DATE", sql
    )
    sql = _DATE_NOW_RE.sub("CURRENT_DATE", sql)
    return sql


# ─── Cursor Wrapper ────────────────────────────────────────────────────────────
class AdaptedCursor:
    """Uniform cursor supporting .fetchone(), .fetchall(), .lastrowid"""

    def __init__(self, raw_cursor, use_pg: bool, col_names=None):
        self._cur      = raw_cursor
        self._use_pg   = use_pg
        self._colnames = col_names  # for pg: passed from execute()
        self._lastrow  = None

    def _wrap(self, raw):
        if raw is None:
            return None
        if isinstance(raw, UniversalRow):
            return raw
        keys = self._colnames or []
        return UniversalRow(keys, list(raw))

    def fetchone(self):
        if self._use_pg:
            row = self._cur.fetchone()
            return self._wrap(row)
        row = self._cur.fetchone()
        if row is None:
            return None
        # sqlite3.Row → UniversalRow
        return UniversalRow(row.keys(), list(row))

    def fetchall(self):
        if self._use_pg:
            rows = self._cur.fetchall()
            return [self._wrap(r) for r in rows]
        rows = self._cur.fetchall()
        return [UniversalRow(r.keys(), list(r)) for r in rows]

    @property
    def lastrowid(self):
        return getattr(self._cur, "lastrowid", None)

    def __iter__(self):
        return iter(self.fetchall())


# ─── SQLite Connection ─────────────────────────────────────────────────────────
def _make_sqlite_conn():
    c = sqlite3.connect(_DB_PATH, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA busy_timeout=20000")
    c.execute("PRAGMA cache_size=-8000")
    c.execute("PRAGMA foreign_keys=ON")
    return c


# ─── PostgreSQL Connection Pool ────────────────────────────────────────────────
_pg_pool       = None
_pg_pool_lock  = threading.Lock()

def _get_pg_pool():
    global _pg_pool
    if _pg_pool is not None:
        return _pg_pool
    with _pg_pool_lock:
        if _pg_pool is None:
            try:
                from psycopg2 import pool as pg_pool
                _pg_pool = pg_pool.ThreadedConnectionPool(
                    minconn=2, maxconn=50, dsn=DATABASE_URL
                )
                log.info("PostgreSQL connection pool created (max=50)")
            except ImportError:
                raise RuntimeError(
                    "psycopg2 not installed. Run: pip install psycopg2-binary"
                )
        return _pg_pool


# ─── Thread-local SQLite ───────────────────────────────────────────────────────
_tlocal  = threading.local()
_db_lock = threading.Lock()


# ─── Unified DB Adapter ────────────────────────────────────────────────────────
class DatabaseAdapter:
    """
    Drop-in replacement for raw sqlite3 connection.
    Use exactly like the old get_db() result, but now also works with PostgreSQL.
    """

    def __init__(self):
        self._use_pg = USE_POSTGRES
        if self._use_pg:
            self._pool = _get_pg_pool()
            self._pg_conn = self._pool.getconn()
            # autocommit off — we commit manually
            self._pg_conn.autocommit = False
        else:
            self._sqlite = _get_sqlite()

    # ── execute ──
    def execute(self, sql: str, params=()):
        if self._use_pg:
            sql = _to_pg(sql)
            cur = self._pg_conn.cursor()
            try:
                cur.execute(sql, params)
            except Exception as e:
                self._pg_conn.rollback()
                raise
            colnames = [d[0] for d in (cur.description or [])]
            return AdaptedCursor(cur, True, colnames)
        else:
            cur = self._sqlite.execute(sql, params)
            return AdaptedCursor(cur, False)

    # ── executemany ──
    def executemany(self, sql: str, params_list):
        if self._use_pg:
            sql = _to_pg(sql)
            cur = self._pg_conn.cursor()
            try:
                cur.executemany(sql, params_list)
            except Exception:
                self._pg_conn.rollback()
                raise
        else:
            self._sqlite.executemany(sql, params_list)

    # ── commit ──
    def commit(self):
        if self._use_pg:
            self._pg_conn.commit()
        else:
            self._sqlite.commit()

    # ── close (returns PG conn to pool) ──
    def close(self):
        if self._use_pg and self._pg_conn:
            try:
                self._pool.putconn(self._pg_conn)
            except Exception:
                pass
            self._pg_conn = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        if exc_type:
            if self._use_pg and self._pg_conn:
                try:
                    self._pg_conn.rollback()
                except Exception:
                    pass
        self.close()


# ─── Thread-local SQLite (for backward-compat get_db()) ──────────────────────
def _get_sqlite():
    if not getattr(_tlocal, "conn", None):
        _tlocal.conn = _make_sqlite_conn()
    return _tlocal.conn

def close_sqlite():
    conn = getattr(_tlocal, "conn", None)
    if conn:
        try:
            conn.close()
        except Exception:
            pass
        _tlocal.conn = None


# ─── Public API ───────────────────────────────────────────────────────────────
def get_db() -> DatabaseAdapter:
    """
    Returns a DatabaseAdapter.
    For SQLite: reuses thread-local connection.
    For PostgreSQL: gets a connection from the pool.
    
    IMPORTANT: For PostgreSQL, call .close() when done (or use as context manager).
    For backward compat, SQLite connections are not closed after each call.
    """
    if USE_POSTGRES:
        return DatabaseAdapter()
    else:
        # SQLite: wrap the thread-local connection in adapter-like interface
        return _SQLiteAdapter(_get_sqlite())


class _SQLiteAdapter:
    """
    Lightweight wrapper around a thread-local sqlite3 connection.
    Same interface as DatabaseAdapter but doesn't need pool management.
    """
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql: str, params=()):
        cur = self._conn.execute(sql, params)
        return AdaptedCursor(cur, False)

    def executemany(self, sql: str, params_list):
        self._conn.executemany(sql, params_list)

    def commit(self):
        self._conn.commit()

    def close(self):
        pass  # SQLite: don't close thread-local conn

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


def close_db():
    """Close thread-local SQLite connection (call at worker thread exit)."""
    close_sqlite()


# ─── PostgreSQL Schema helpers ─────────────────────────────────────────────────
def pg_add_column_if_missing(db, table, col, col_type):
    """Safe ALTER TABLE ADD COLUMN — ignores if column already exists."""
    if USE_POSTGRES:
        try:
            db.execute(
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {col_type}"
            )
            db.commit()
        except Exception:
            pass
    else:
        try:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
            db.commit()
        except Exception:
            pass
