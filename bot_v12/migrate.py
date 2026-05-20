"""
migrate.py — Schema Migration: v9 → v10

Migrates old fb_pages schema to new architecture:
  OLD: fb_pages with UNIQUE(account_id, page_id)  → duplicate pages
  NEW: pages (global) + account_pages (relation)  → deduplicated

Safe to run multiple times (idempotent).
Run ONCE before starting the upgraded bot.

Usage:
  python migrate.py
  python migrate.py --db /path/to/your/data.db
"""

import sqlite3
import sys
import os
import argparse
from datetime import datetime, timezone


def _now():
    return datetime.now(timezone.utc).isoformat()


def migrate(db_path: str = "data.db"):
    print(f"\n{'='*60}")
    print("  Bot v10 — Database Migration")
    print(f"  DB: {os.path.abspath(db_path)}")
    print(f"{'='*60}\n")

    if not os.path.exists(db_path):
        print(f"  ✗ Database not found: {db_path}")
        print("    Bot pehle ek baar chalao taake DB create ho, phir migrate karo.")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    # ── Step 1: Create new tables (if not exist) ──────────────────────────────
    print("  [1/5] Creating new tables (pages, account_pages, structured_logs)...")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS pages (
            page_id            TEXT PRIMARY KEY,
            page_name          TEXT DEFAULT '',
            category           TEXT DEFAULT '',
            followers          INTEGER DEFAULT 0,
            primary_account_id INTEGER,
            primary_token      TEXT DEFAULT '',
            token_updated_at   TEXT DEFAULT '',
            in_use             INTEGER DEFAULT 0
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS account_pages (
            account_id INTEGER NOT NULL,
            page_id    TEXT NOT NULL,
            page_token TEXT DEFAULT '',
            is_primary INTEGER DEFAULT 0,
            added_at   TEXT DEFAULT '',
            PRIMARY KEY (account_id, page_id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS structured_logs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER,
            level      TEXT,
            request_id TEXT,
            message    TEXT,
            extra      TEXT,
            ts         TEXT
        )
    """)

    # Indexes
    for idx in [
        "CREATE INDEX IF NOT EXISTS idx_pages_primary ON pages(primary_account_id)",
        "CREATE INDEX IF NOT EXISTS idx_ap_account ON account_pages(account_id)",
        "CREATE INDEX IF NOT EXISTS idx_ap_page ON account_pages(page_id)",
        "CREATE INDEX IF NOT EXISTS idx_slog_ch ON structured_logs(channel_id, ts DESC)",
    ]:
        try:
            conn.execute(idx)
        except Exception:
            pass

    conn.commit()
    print("    ✓ Tables created")

    # ── Step 2: Check old fb_pages ────────────────────────────────────────────
    print("\n  [2/5] Reading old fb_pages data...")

    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]

    if "fb_pages" not in tables:
        print("    ℹ  fb_pages table not found — nothing to migrate")
    else:
        old_rows = conn.execute(
            "SELECT account_id, page_id, page_name, page_token, category, followers, in_use "
            "FROM fb_pages"
        ).fetchall()
        print(f"    Found {len(old_rows)} rows in fb_pages")

        # ── Step 3: Migrate to new tables ─────────────────────────────────────
        print("\n  [3/5] Migrating to pages + account_pages...")

        migrated = 0
        skipped  = 0
        conflicts = []

        for row in old_rows:
            account_id = row[0]
            page_id    = row[1]
            page_name  = row[2] or ""
            page_token = row[3] or ""
            category   = row[4] or ""
            followers  = row[5] or 0
            in_use     = row[6] or 0

            try:
                # Insert into global pages
                existing = conn.execute(
                    "SELECT primary_account_id FROM pages WHERE page_id=?",
                    (page_id,)
                ).fetchone()

                if not existing:
                    conn.execute("""
                        INSERT INTO pages
                            (page_id, page_name, category, followers,
                             primary_account_id, primary_token, token_updated_at, in_use)
                        VALUES (?,?,?,?,?,?,?,?)
                    """, (page_id, page_name, category, followers,
                          account_id, page_token, _now(), in_use))
                    is_primary = 1
                else:
                    old_owner = existing[0]
                    is_primary = 1 if old_owner == account_id else 0
                    if old_owner != account_id:
                        conflicts.append((page_id, page_name, old_owner, account_id))

                # Insert into account_pages
                conn.execute("""
                    INSERT INTO account_pages
                        (account_id, page_id, page_token, is_primary, added_at)
                    VALUES (?,?,?,?,?)
                    ON CONFLICT(account_id, page_id) DO NOTHING
                """, (account_id, page_id, page_token, is_primary, _now()))

                migrated += 1
            except Exception as e:
                print(f"    ✗ Error migrating page {page_id}: {e}")
                skipped += 1

        conn.commit()
        print(f"    ✓ Migrated: {migrated} rows")
        if skipped:
            print(f"    ⚠  Skipped:  {skipped} rows (errors)")
        if conflicts:
            print(f"\n    ⚠  Detected {len(conflicts)} pages with multiple accounts:")
            for pid, pname, old, new in conflicts[:10]:
                print(f"       Page {pid} ({pname}): accounts {old} + {new}")
            if len(conflicts) > 10:
                print(f"       ... and {len(conflicts)-10} more")

    # ── Step 4: Verify global uniqueness ──────────────────────────────────────
    print("\n  [4/5] Verifying global page uniqueness...")

    total_pages   = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
    unique_pages  = conn.execute("SELECT COUNT(DISTINCT page_id) FROM pages").fetchone()[0]
    total_ap      = conn.execute("SELECT COUNT(*) FROM account_pages").fetchone()[0]

    print(f"    pages table:         {total_pages} rows (should equal unique page IDs)")
    print(f"    account_pages table: {total_ap} rows")

    if total_pages == unique_pages:
        print("    ✓ No duplicates — global uniqueness confirmed")
    else:
        print(f"    ✗ Duplicate pages detected! ({total_pages - unique_pages} extras)")

    # ── Step 5: Token conflict summary ────────────────────────────────────────
    print("\n  [5/5] Token conflict analysis...")

    multi_account = conn.execute("""
        SELECT page_id, COUNT(account_id) as cnt
        FROM account_pages
        GROUP BY page_id HAVING cnt > 1
    """).fetchall()

    if multi_account:
        print(f"    ⚠  {len(multi_account)} pages have tokens from multiple accounts")
        print("       → Primary token (newest) has been set for each")
        print("       → Use /api/pages/conflicts to review")
    else:
        print("    ✓ No token conflicts detected")

    conn.close()

    print(f"\n{'='*60}")
    print("  ✅ Migration complete!")
    print("  You can now start bot_v10 safely.")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate bot database v9 → v10")
    parser.add_argument("--db", default="data.db", help="Path to SQLite database")
    args = parser.parse_args()
    migrate(args.db)
