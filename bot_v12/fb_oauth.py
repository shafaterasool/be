"""
fb_oauth.py — Facebook OAuth: login, page management, multi-account manager
v10 ARCHITECTURE UPGRADE:

  OLD (broken):
    fb_pages: UNIQUE(account_id, page_id) → same page appears in MULTIPLE rows
    → User dekhta hai duplicate entries
    → Token conflicts: kaunsa token valid hai? System nahi jaanta
    → No global ownership: page kisi bhi account ke saath exist kar sakta hai

  NEW (industry-standard):
    pages          → UNIQUE(page_id) — har page globally ek baar
    account_pages  → Many-to-many bridge (account_id, page_id)
    fb_accounts    → Unchanged

  Global Page Ownership Logic:
    → Jab naya account same page add kare: CONFLICT detect hota hai
    → System check karta hai: konsa token newest hai
    → pages.primary_account_id = best token wala account
    → pages.primary_token = woh best token (denormalized for performance)
    → Token conflicts automatically resolve hote hain newest token se

  Token Validation:
    → Upload se pehle token validate karo (FB API ping)
    → Invalid token hone pe: dusra account ka token try karo

  Structured Logging:
    → Har operation ka request_id
    → Stack traces on errors
    → Token events log hote hain

  Auto Graph API Version (v10.1 addition):
    → GRAPH URL ab hardcoded nahi hai
    → Facebook ke discovery endpoint se latest version auto-fetch hoti hai
    → app_config DB mein 7 din ke liye cached rehti hai
    → Agar FB down → last saved version use hoti hai
    → Agar bilkul fresh → FALLBACK_VERSION use hoti hai
"""

import requests
import logging
import traceback
import threading
from datetime import datetime, timezone, timedelta

from db import get_db, _db_lock
from structured_logger import StructuredLogger, get_logger, new_request_id

log = logging.getLogger("fb_oauth")

# ══════════════════════════════════════════════════════════════════════════════
# DYNAMIC GRAPH API VERSION — auto-fetch, DB-cached, fallback-safe
# ══════════════════════════════════════════════════════════════════════════════

_FB_DISCOVERY_URL  = "https://graph.facebook.com/"
_FALLBACK_VERSION  = "v21.0"
_REFRESH_DAYS      = 7
_DB_KEY_VERSION    = "fb_api_version"
_DB_KEY_CHECKED_AT = "fb_api_version_checked_at"


def _fetch_fb_version_from_api() -> str | None:
    """
    Facebook ke discovery endpoint se latest version fetch karo.
    GET https://graph.facebook.com/  →  {"api_version": "v21.0", ...}
    Returns version string ya None on failure.
    """
    try:
        r = requests.get(_FB_DISCOVERY_URL, timeout=8)
        data = r.json()
        version = data.get("api_version") or data.get("version")
        if version and isinstance(version, str) and version.startswith("v"):
            return version
        log.debug(f"Unexpected FB discovery response: {data}")
        return None
    except requests.exceptions.Timeout:
        log.warning("FB version fetch timed out")
        return None
    except requests.exceptions.ConnectionError:
        log.warning("FB version fetch — no connection")
        return None
    except Exception as e:
        log.warning(f"FB version fetch error: {e}")
        return None


def _load_version_from_db() -> str | None:
    try:
        row = get_db().execute(
            "SELECT value FROM app_config WHERE key=?", (_DB_KEY_VERSION,)
        ).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def _load_checked_at_from_db() -> str | None:
    try:
        row = get_db().execute(
            "SELECT value FROM app_config WHERE key=?", (_DB_KEY_CHECKED_AT,)
        ).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def _save_version_to_db(version: str, update_checked_at: bool = True):
    try:
        now = datetime.now(timezone.utc).isoformat()
        with _db_lock:
            db = get_db()
            if version:
                db.execute(
                    "INSERT INTO app_config (key, value) VALUES (?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (_DB_KEY_VERSION, version)
                )
            if update_checked_at:
                db.execute(
                    "INSERT INTO app_config (key, value) VALUES (?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (_DB_KEY_CHECKED_AT, now)
                )
            db.commit()
    except Exception as e:
        log.error(f"Failed to save FB version to DB: {e}")


def get_graph_url() -> str:
    """
    FB Graph API ka full base URL return karta hai.
    Automatically latest version use karta hai, 7 din mein refresh hota hai.

    Usage:
        r = requests.get(f"{get_graph_url()}/me", params={...})
        r = requests.get(f"{get_graph_url()}/oauth/access_token", params={...})
    """
    cached_version  = _load_version_from_db()
    last_checked_at = _load_checked_at_from_db()

    # Check: kya refresh karna chahiye?
    needs_refresh = True
    if last_checked_at:
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(last_checked_at)
            if age < timedelta(days=_REFRESH_DAYS):
                needs_refresh = False
        except (ValueError, TypeError):
            pass

    if not needs_refresh and cached_version:
        return f"https://graph.facebook.com/{cached_version}"

    # Refresh attempt
    fresh_version = _fetch_fb_version_from_api()

    if fresh_version:
        _save_version_to_db(fresh_version)
        log.info(f"FB Graph API version updated: {fresh_version}")
        return f"https://graph.facebook.com/{fresh_version}"

    if cached_version:
        log.warning(f"FB version refresh failed — using cached: {cached_version}")
        _save_version_to_db("", update_checked_at=True)  # timestamp update, version nahi
        return f"https://graph.facebook.com/{cached_version}"

    log.warning(f"FB version unknown — using fallback: {_FALLBACK_VERSION}")
    return f"https://graph.facebook.com/{_FALLBACK_VERSION}"


def _now():
    return datetime.now(timezone.utc).isoformat()


# ══════════════════════════════════════════════════════════════════════════════
# DB TABLES — Industry-Standard Architecture
# ══════════════════════════════════════════════════════════════════════════════
def init_oauth_tables():
    """
    Create all OAuth tables.
    Safe to call multiple times (idempotent).
    Migrates old fb_pages data to new schema automatically.
    """
    logger = get_logger()
    db = get_db()

    # ── fb_accounts (unchanged) ──────────────────────────────────────────────
    db.execute("""
        CREATE TABLE IF NOT EXISTS fb_accounts (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            fb_user_id    TEXT UNIQUE NOT NULL,
            fb_name       TEXT DEFAULT '',
            fb_email      TEXT DEFAULT '',
            user_token    TEXT DEFAULT '',
            app_id        TEXT DEFAULT '',
            app_secret    TEXT DEFAULT '',
            connected_at  TEXT DEFAULT '',
            active        INTEGER DEFAULT 1
        )
    """)

    # ── pages — GLOBAL unique pages ───────────────────────────────────────────
    db.execute("""
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

    # ── account_pages — Many-to-many relationship ─────────────────────────────
    db.execute("""
        CREATE TABLE IF NOT EXISTS account_pages (
            account_id   INTEGER NOT NULL,
            page_id      TEXT NOT NULL,
            page_token   TEXT DEFAULT '',
            is_primary   INTEGER DEFAULT 0,
            added_at     TEXT DEFAULT '',
            PRIMARY KEY (account_id, page_id)
        )
    """)

    # ── app_config ────────────────────────────────────────────────────────────
    db.execute("""
        CREATE TABLE IF NOT EXISTS app_config (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    # ── Indexes ───────────────────────────────────────────────────────────────
    for idx_sql in [
        "CREATE INDEX IF NOT EXISTS idx_pages_primary_account ON pages(primary_account_id)",
        "CREATE INDEX IF NOT EXISTS idx_account_pages_account ON account_pages(account_id)",
        "CREATE INDEX IF NOT EXISTS idx_account_pages_page ON account_pages(page_id)",
        "CREATE INDEX IF NOT EXISTS idx_account_pages_primary ON account_pages(page_id, is_primary)",
    ]:
        try:
            db.execute(idx_sql)
        except Exception:
            pass

    db.commit()

    # ── Migrate old fb_pages → new schema ────────────────────────────────────
    _migrate_old_fb_pages(db, logger)

    logger.info("OAuth tables initialized (pages + account_pages architecture)")


def _migrate_old_fb_pages(db, logger):
    """
    One-time migration: copy old fb_pages rows into new pages + account_pages tables.
    Safe to run multiple times — skips already-migrated rows.
    """
    try:
        old_rows = db.execute(
            "SELECT account_id, page_id, page_name, page_token, category, followers "
            "FROM fb_pages"
        ).fetchall()
    except Exception:
        return

    if not old_rows:
        return

    migrated = 0
    for row in old_rows:
        try:
            account_id = row[0]
            page_id    = row[1]
            page_name  = row[2] or ""
            page_token = row[3] or ""
            category   = row[4] or ""
            followers  = row[5] or 0

            db.execute("""
                INSERT INTO pages (page_id, page_name, category, followers,
                                   primary_account_id, primary_token, token_updated_at)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(page_id) DO NOTHING
            """, (page_id, page_name, category, followers,
                  account_id, page_token, _now()))

            db.execute("""
                INSERT INTO account_pages (account_id, page_id, page_token, is_primary, added_at)
                VALUES (?,?,?,1,?)
                ON CONFLICT(account_id, page_id) DO NOTHING
            """, (account_id, page_id, page_token, _now()))

            migrated += 1
        except Exception as e:
            log.warning(f"Migration row skip: {e}")

    if migrated:
        db.commit()
        logger.info(f"Migrated {migrated} old fb_pages rows → new schema")


# ══════════════════════════════════════════════════════════════════════════════
# APP CONFIG
# ══════════════════════════════════════════════════════════════════════════════
def set_config(key, value):
    with _db_lock:
        db = get_db()
        db.execute(
            "INSERT INTO app_config (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value)
        )
        db.commit()


def get_config(key, default=None):
    row = get_db().execute(
        "SELECT value FROM app_config WHERE key=?", (key,)
    ).fetchone()
    return row[0] if row else default


# ══════════════════════════════════════════════════════════════════════════════
# OAUTH URL + TOKEN EXCHANGE
# ══════════════════════════════════════════════════════════════════════════════
def build_oauth_url(app_id, redirect_uri):
    # Version dynamically nikalta hai get_graph_url() se — hardcoded nahi
    version = get_graph_url().split("/")[-1]   # e.g. "v21.0"
    scope = ",".join([
        "email",
        "pages_show_list",
        "pages_read_engagement",
        "pages_manage_posts",
    ])
    return (
        f"https://www.facebook.com/{version}/dialog/oauth"
        f"?client_id={app_id}"
        f"&redirect_uri={redirect_uri}"
        f"&scope={scope}"
        f"&response_type=code"
    )


def exchange_code_for_token(app_id, app_secret, redirect_uri, code):
    logger = get_logger()
    try:
        r = requests.get(f"{get_graph_url()}/oauth/access_token", params={
            "client_id":     app_id,
            "client_secret": app_secret,
            "redirect_uri":  redirect_uri,
            "code":          code,
        }, timeout=15)
        r.raise_for_status()
        logger.info("OAuth code exchanged for token")
        return r.json().get("access_token")
    except Exception as e:
        logger.error("OAuth code exchange failed", exc=e)
        raise


def extend_token(app_id, app_secret, short_token):
    """
    Token ko 60-day long-lived token mein extend karo.
    Agar extension fail ho (wrong app / expired) to original token wapas karo —
    bot band nahi hoga, existing token se kaam karta rahega.
    """
    logger = get_logger()
    try:
        r = requests.get(f"{get_graph_url()}/oauth/access_token", params={
            "grant_type":        "fb_exchange_token",
            "client_id":         app_id,
            "client_secret":     app_secret,
            "fb_exchange_token": short_token,
        }, timeout=15)
        r.raise_for_status()
        logger.info("Short-lived token extended to 60-day token")
        return r.json().get("access_token", short_token)
    except Exception as e:
        # ✅ FIX: Extension fail hone par bot nahi rukta — purana token use karo
        err_msg = str(e)
        if "200" in err_msg or "OAuthException" in err_msg:
            logger.warning(
                "Token extension failed (wrong app or permissions). "
                "Existing token use ho raha hai — manually reconnect karein."
            )
        else:
            logger.warning(f"Token extension failed: {e}. Existing token use ho raha hai.")
        return short_token  # ← crash mat karo, kaam chalta rahe


def get_user_info(user_token):
    r = requests.get(f"{get_graph_url()}/me", params={
        "fields": "id,name,email",
        "access_token": user_token,
    }, timeout=15)
    r.raise_for_status()
    return r.json()


# ══════════════════════════════════════════════════════════════════════════════
# FETCH USER PAGES
# ══════════════════════════════════════════════════════════════════════════════
def fetch_user_pages(user_token):
    """
    Fetch all FB Pages the user manages.
    Returns list of dicts with page_id, page_name, page_token, followers, category.
    """
    logger = get_logger()
    pages = []
    url   = f"{get_graph_url()}/me/accounts"

    while url:
        r = requests.get(url, params={
            "fields":       "id,name,access_token,fan_count,category",
            "access_token": user_token,
            "limit":        100,
        }, timeout=15)

        if r.status_code != 200:
            logger.error(f"Pages fetch error: {r.text}", status_code=r.status_code)
            break

        data = r.json()

        # ✅ FIX: Facebook kabhi kabhi HTTP 200 deta hai lekin JSON mein error hota hai
        if "error" in data:
            fb_err  = data["error"]
            fb_msg  = fb_err.get("message", "Unknown FB error")
            fb_code = fb_err.get("code")
            logger.error(f"Pages fetch error: {data}")
            if fb_code == 200 or fb_err.get("type") == "OAuthException":
                raise PermissionError(
                    f"FB App Permission Error (code {fb_code}): "
                    f"Is account ko App mein 'Developer' ya 'Tester' role chahiye. "
                    f"Meta for Developers → App Roles mein add karo, "
                    f"ya app ko Live mode mein publish karo. "
                    f"FB detail: {fb_msg}"
                )
            break

        for pg in data.get("data", []):
            pages.append({
                "page_id":    pg.get("id"),
                "page_name":  pg.get("name"),
                "page_token": pg.get("access_token"),
                "followers":  pg.get("fan_count") or 0,
                "category":   pg.get("category") or "",
            })
        url = data.get("paging", {}).get("next")

    logger.info(f"Fetched {len(pages)} pages from Facebook")
    return pages


# ══════════════════════════════════════════════════════════════════════════════
# GLOBAL PAGE OWNERSHIP LOGIC (CORE ARCHITECTURE)
# ══════════════════════════════════════════════════════════════════════════════
def _claim_page_ownership(db, logger, account_id, page_id, page_name,
                          page_token, category, followers):
    """
    Industry-standard page ownership logic:

    1. Check if page already exists globally
    2. If not → insert, this account is primary owner
    3. If yes → update account_pages with new token
       → Compare token_updated_at: newer token becomes primary
    4. Always maintain: pages.primary_token = best valid token
    """
    now = _now()

    existing = db.execute(
        "SELECT primary_account_id, token_updated_at FROM pages WHERE page_id=?",
        (page_id,)
    ).fetchone()

    if not existing:
        db.execute("""
            INSERT INTO pages
                (page_id, page_name, category, followers,
                 primary_account_id, primary_token, token_updated_at, in_use)
            VALUES (?,?,?,?,?,?,?,0)
            ON CONFLICT(page_id) DO UPDATE SET
                page_name          = excluded.page_name,
                category           = excluded.category,
                followers          = excluded.followers,
                primary_account_id = excluded.primary_account_id,
                primary_token      = excluded.primary_token,
                token_updated_at   = excluded.token_updated_at
        """, (page_id, page_name, category, followers,
              account_id, page_token, now))

        db.execute("""
            INSERT INTO account_pages (account_id, page_id, page_token, is_primary, added_at)
            VALUES (?,?,?,1,?)
            ON CONFLICT(account_id, page_id) DO UPDATE SET
                page_token = excluded.page_token,
                is_primary = 1,
                added_at   = excluded.added_at
        """, (account_id, page_id, page_token, now))

        logger.ownership_claimed(page_id, account_id)

    else:
        old_owner = existing[0]

        db.execute("""
            INSERT INTO account_pages (account_id, page_id, page_token, is_primary, added_at)
            VALUES (?,?,?,0,?)
            ON CONFLICT(account_id, page_id) DO UPDATE SET
                page_token = excluded.page_token,
                added_at   = excluded.added_at
        """, (account_id, page_id, page_token, now))

        db.execute("""
            UPDATE pages SET
                page_name          = ?,
                category           = ?,
                followers          = ?,
                primary_account_id = ?,
                primary_token      = ?,
                token_updated_at   = ?
            WHERE page_id=?
              AND (primary_account_id = ?
                   OR token_updated_at < ?)
        """, (page_name, category, followers,
              account_id, page_token, now,
              page_id,
              account_id, now))

        if old_owner != account_id:
            db.execute(
                "UPDATE account_pages SET is_primary=0 "
                "WHERE page_id=? AND account_id=?",
                (page_id, old_owner)
            )
            db.execute(
                "UPDATE account_pages SET is_primary=1 "
                "WHERE page_id=? AND account_id=?",
                (page_id, account_id)
            )
            logger.ownership_claimed(page_id, account_id, was_owned_by=old_owner)
        else:
            db.execute(
                "UPDATE account_pages SET is_primary=1, page_token=? "
                "WHERE page_id=? AND account_id=?",
                (page_token, page_id, account_id)
            )


# ══════════════════════════════════════════════════════════════════════════════
# SAVE ACCOUNT + PAGES
# ══════════════════════════════════════════════════════════════════════════════
def save_account(fb_user_id, fb_name, fb_email, user_token,
                 app_id, app_secret, pages):
    """
    Save FB account and all its pages using new schema.
    Returns account_id.
    """
    logger = get_logger()

    with _db_lock:
        db = get_db()
        db.execute("""
            INSERT INTO fb_accounts
                (fb_user_id, fb_name, fb_email, user_token,
                 app_id, app_secret, connected_at, active)
            VALUES (?,?,?,?,?,?,?,1)
            ON CONFLICT(fb_user_id) DO UPDATE SET
                fb_name      = excluded.fb_name,
                fb_email     = excluded.fb_email,
                user_token   = excluded.user_token,
                app_id       = excluded.app_id,
                app_secret   = excluded.app_secret,
                connected_at = excluded.connected_at,
                active       = 1
        """, (fb_user_id, fb_name, fb_email, user_token,
              app_id, app_secret, _now()))
        db.commit()

    account_id = get_db().execute(
        "SELECT id FROM fb_accounts WHERE fb_user_id=?", (fb_user_id,)
    ).fetchone()[0]

    with _db_lock:
        db = get_db()
        for pg in pages:
            _claim_page_ownership(
                db, logger,
                account_id,
                pg["page_id"],
                pg["page_name"],
                pg["page_token"],
                pg["category"],
                pg["followers"],
            )
        db.commit()

    logger.info(
        f"Account saved: {fb_name} ({fb_user_id}) with {len(pages)} pages",
        account_id=account_id,
        page_count=len(pages),
    )
    return account_id


def save_manual_account(account_name, account_email, page_name,
                        page_id, page_token, category="Manual", followers=0):
    """
    Save a manually provided page token.
    Uses same global ownership logic — no duplicates possible.
    """
    logger = get_logger()
    account_key = (account_email or account_name or page_id or "").strip().lower()
    fb_user_id  = f"manual:{account_key}"
    page_name   = page_name or account_name

    with _db_lock:
        db = get_db()
        db.execute("""
            INSERT INTO fb_accounts
                (fb_user_id, fb_name, fb_email, user_token,
                 app_id, app_secret, connected_at, active)
            VALUES (?,?,?,?,?,?,?,1)
            ON CONFLICT(fb_user_id) DO UPDATE SET
                fb_name      = excluded.fb_name,
                fb_email     = excluded.fb_email,
                connected_at = excluded.connected_at,
                active       = 1
        """, (fb_user_id, account_name, account_email or "",
              "", "", "", _now()))
        db.commit()

    account_id = get_db().execute(
        "SELECT id FROM fb_accounts WHERE fb_user_id=?", (fb_user_id,)
    ).fetchone()[0]

    with _db_lock:
        db = get_db()
        _claim_page_ownership(
            db, logger,
            account_id, page_id, page_name,
            page_token, category or "Manual", followers or 0
        )
        db.commit()

    logger.info(
        f"Manual account saved: {account_name} [{page_id}]",
        account_id=account_id,
        page_id=page_id,
    )
    return account_id


# ══════════════════════════════════════════════════════════════════════════════
# READ ACCOUNTS + PAGES
# ══════════════════════════════════════════════════════════════════════════════
def get_all_accounts():
    """
    Returns all accounts with their pages.
    Each page shows globally: which account is primary, token freshness.
    No duplicate pages shown to user.
    """
    rows = get_db().execute(
        "SELECT id, fb_user_id, fb_name, fb_email, connected_at, active "
        "FROM fb_accounts ORDER BY id"
    ).fetchall()

    accounts = []
    for r in rows:
        acc = {
            "id":           r[0],
            "fb_user_id":   r[1],
            "fb_name":      r[2],
            "fb_email":     r[3] or "",
            "connected_at": r[4],
            "active":       bool(r[5]),
            "is_manual":    bool((r[1] or "").startswith("manual:")),
        }

        page_rows = get_db().execute("""
            SELECT
                p.page_id,
                p.page_name,
                ap.page_token,
                p.followers,
                p.category,
                p.in_use,
                ap.is_primary,
                p.primary_account_id,
                p.token_updated_at
            FROM account_pages ap
            JOIN pages p ON p.page_id = ap.page_id
            WHERE ap.account_id = ?
            ORDER BY p.followers DESC
        """, (r[0],)).fetchall()

        acc["pages"] = [{
            "page_id":            p[0],
            "page_name":          p[1],
            "page_token":         (p[2] or "")[:10] + "..." if p[2] else "",
            "followers":          p[3] or 0,
            "category":           p[4] or "",
            "in_use":             bool(p[5]),
            "is_primary_owner":   bool(p[6]),
            "primary_account_id": p[7],
            "token_updated_at":   p[8] or "",
        } for p in page_rows]

        acc["page_count"] = len(acc["pages"])
        accounts.append(acc)

    return accounts


def get_global_pages():
    """
    Get all unique pages globally (across all accounts).
    Each page appears ONCE with its primary owner info.
    Used for dashboard deduplication view.
    """
    rows = get_db().execute("""
        SELECT
            p.page_id,
            p.page_name,
            p.category,
            p.followers,
            p.in_use,
            p.primary_account_id,
            p.token_updated_at,
            a.fb_name AS primary_account_name,
            COUNT(ap.account_id) AS account_count
        FROM pages p
        LEFT JOIN fb_accounts a ON a.id = p.primary_account_id
        LEFT JOIN account_pages ap ON ap.page_id = p.page_id
        GROUP BY p.page_id
        ORDER BY p.followers DESC
    """).fetchall()

    return [{
        "page_id":              r[0],
        "page_name":            r[1],
        "category":             r[2] or "",
        "followers":            r[3] or 0,
        "in_use":               bool(r[4]),
        "primary_account_id":   r[5],
        "token_updated_at":     r[6] or "",
        "primary_account_name": r[7] or "Unknown",
        "account_count":        r[8] or 0,
    } for r in rows]


# ══════════════════════════════════════════════════════════════════════════════
# TOKEN MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════
def get_page_token(page_id: str) -> str:
    """
    Get the best (primary) token for a page.
    Uses pages.primary_token (denormalized, always the freshest valid token).
    Falls back to any account's token if primary is missing.
    """
    row = get_db().execute(
        "SELECT primary_token FROM pages WHERE page_id=?",
        (page_id,)
    ).fetchone()

    if row and row[0]:
        return row[0]

    fb = get_db().execute(
        "SELECT page_token FROM account_pages WHERE page_id=? AND page_token != '' LIMIT 1",
        (page_id,)
    ).fetchone()
    return fb[0] if fb else None


def validate_page_token(page_id: str, page_token: str, logger=None) -> dict:
    """
    Validate a page token against the Facebook API.
    Returns: {"valid": bool, "reason": str, "page_name": str}
    """
    lg = logger or get_logger()
    try:
        r = requests.get(f"{get_graph_url()}/{page_id}", params={
            "fields":       "id,name",
            "access_token": page_token,
        }, timeout=10)

        if r.status_code == 200:
            data = r.json()
            if "error" in data:
                reason = data["error"].get("message", "Unknown error")
                lg.token_validated(page_id, False, reason=reason)
                return {"valid": False, "reason": reason, "page_name": ""}
            lg.token_validated(page_id, True)
            return {"valid": True, "reason": "OK", "page_name": data.get("name", "")}

        reason = f"HTTP {r.status_code}"
        lg.token_validated(page_id, False, reason=reason)
        return {"valid": False, "reason": reason, "page_name": ""}

    except Exception as e:
        reason = str(e)
        lg.token_validated(page_id, False, reason=reason)
        return {"valid": False, "reason": reason, "page_name": ""}


def get_best_token_for_page(page_id: str, logger=None) -> str:
    """
    Smart token resolution:
    1. Try primary token → validate
    2. If invalid, try other accounts' tokens
    3. Promote whichever token works
    Returns best valid token, or primary token if none validate.
    """
    lg = logger or get_logger()

    rows = get_db().execute("""
        SELECT ap.account_id, ap.page_token, ap.is_primary
        FROM account_pages ap
        WHERE ap.page_id = ? AND ap.page_token != ''
        ORDER BY ap.is_primary DESC, ap.added_at DESC
    """, (page_id,)).fetchall()

    if not rows:
        return None

    for row in rows:
        account_id = row[0]
        token      = row[1]
        if not token:
            continue

        result = validate_page_token(page_id, token, lg)
        if result["valid"]:
            if not row[2]:
                lg.info(
                    f"Promoting account {account_id} as primary owner for page {page_id}",
                    page_id=page_id, account_id=account_id
                )
                with _db_lock:
                    db = get_db()
                    db.execute(
                        "UPDATE pages SET primary_account_id=?, primary_token=?, token_updated_at=? WHERE page_id=?",
                        (account_id, token, _now(), page_id)
                    )
                    db.execute(
                        "UPDATE account_pages SET is_primary=0 WHERE page_id=?", (page_id,)
                    )
                    db.execute(
                        "UPDATE account_pages SET is_primary=1 WHERE page_id=? AND account_id=?",
                        (page_id, account_id)
                    )
                    db.commit()
            return token

    lg.warning(
        f"No valid token found for page {page_id} — using primary token as fallback",
        page_id=page_id
    )
    return rows[0][1]


# ══════════════════════════════════════════════════════════════════════════════
# ACCOUNT OPERATIONS
# ══════════════════════════════════════════════════════════════════════════════
def disconnect_account(account_id: int):
    with _db_lock:
        db = get_db()
        db.execute("UPDATE fb_accounts SET active=0 WHERE id=?", (account_id,))
        db.commit()

    orphaned = get_db().execute(
        "SELECT page_id FROM pages WHERE primary_account_id=?", (account_id,)
    ).fetchall()

    if orphaned:
        logger = get_logger()
        with _db_lock:
            db = get_db()
            for row in orphaned:
                pid = row[0]
                alt = db.execute("""
                    SELECT ap.account_id, ap.page_token
                    FROM account_pages ap
                    JOIN fb_accounts a ON a.id = ap.account_id
                    WHERE ap.page_id=? AND ap.account_id != ? AND a.active=1
                    ORDER BY ap.added_at DESC LIMIT 1
                """, (pid, account_id)).fetchone()

                if alt:
                    db.execute(
                        "UPDATE pages SET primary_account_id=?, primary_token=? WHERE page_id=?",
                        (alt[0], alt[1], pid)
                    )
                    db.execute(
                        "UPDATE account_pages SET is_primary=0 WHERE page_id=?", (pid,)
                    )
                    db.execute(
                        "UPDATE account_pages SET is_primary=1 WHERE page_id=? AND account_id=?",
                        (pid, alt[0])
                    )
                    logger.info(
                        f"Page {pid} ownership transferred to account {alt[0]} after disconnect",
                        page_id=pid, new_account=alt[0]
                    )
                else:
                    logger.warning(
                        f"Page {pid} has no alternative owner after account {account_id} disconnect",
                        page_id=pid
                    )
            db.commit()


def delete_account(account_id: int):
    """Permanently delete an account and all its data from the database."""
    logger = get_logger()
    with _db_lock:
        db = get_db()
        # Remove from account_pages link table
        db.execute("DELETE FROM account_pages WHERE account_id=?", (account_id,))
        # Remove the account itself
        db.execute("DELETE FROM fb_accounts WHERE id=?", (account_id,))
        db.commit()
    logger.info(f"Account {account_id} permanently deleted", account_id=account_id)


def refresh_manual_token(account_id: int, page_id: str, new_token: str):
    """Update token for a manually added account page."""
    logger = get_logger()
    now = _now()
    with _db_lock:
        db = get_db()
        db.execute(
            "UPDATE account_pages SET page_token=? WHERE account_id=? AND page_id=?",
            (new_token, account_id, page_id)
        )
        db.execute(
            "UPDATE pages SET primary_token=?, token_updated_at=? WHERE page_id=?",
            (new_token, now, page_id)
        )
        # ✅ channels table bhi update karo — bot yahi token use karta hai
        db.execute(
            "UPDATE channels SET fb_token=? WHERE fb_page_id=?",
            (new_token, page_id)
        )
        # ✅ legacy fb_pages bhi update karo
        db.execute(
            "UPDATE fb_pages SET page_token=? WHERE page_id=?",
            (new_token, page_id)
        )
        db.execute(
            "INSERT INTO token_refresh_log(channel_id, refreshed_at, status, note) "
            "SELECT id, ?, 'success', 'Manual token updated' FROM channels WHERE fb_page_id=?",
            (now, page_id)
        )
        db.commit()
    logger.info(f"Manual token updated for account {account_id}, page {page_id}", account_id=account_id, page_id=page_id)


def refresh_account_pages(account_id: int):
    """Refresh pages for an account — re-fetch from FB and update tokens."""
    logger = get_logger()

    row = get_db().execute(
        "SELECT fb_user_id, user_token, app_id, app_secret, fb_name "
        "FROM fb_accounts WHERE id=?",
        (account_id,)
    ).fetchone()

    if not row:
        return 0

    fb_user_id, user_token, app_id, app_secret, fb_name = (
        row[0], row[1], row[2], row[3], row[4]
    )

    # ✅ FIX: extend_token ab crash nahi karta — fallback token milta hai
    user_token = extend_token(app_id, app_secret, user_token)
    with _db_lock:
        db = get_db()
        db.execute(
            "UPDATE fb_accounts SET user_token=? WHERE id=?",
            (user_token, account_id)
        )
        db.commit()
    logger.info(f"Token processed for account {account_id}")

    pages = fetch_user_pages(user_token)

    with _db_lock:
        db = get_db()
        for pg in pages:
            _claim_page_ownership(
                db, logger,
                account_id,
                pg["page_id"],
                pg["page_name"],
                pg["page_token"],
                pg["category"],
                pg["followers"],
            )
        db.commit()

    logger.info(
        f"Account {account_id} ({fb_name}) refreshed: {len(pages)} pages",
        account_id=account_id, page_count=len(pages)
    )
    return len(pages)


def mark_page_in_use(page_id: str, in_use: bool = True):
    """Mark a page as in-use (being used by a channel)."""
    with _db_lock:
        db = get_db()
        db.execute(
            "UPDATE pages SET in_use=? WHERE page_id=?",
            (1 if in_use else 0, page_id)
        )
        db.commit()


def get_page_info(page_id: str) -> dict:
    """Get full info for a page from global pages table."""
    row = get_db().execute("""
        SELECT p.page_id, p.page_name, p.category, p.followers,
               p.primary_account_id, p.token_updated_at, p.in_use,
               a.fb_name
        FROM pages p
        LEFT JOIN fb_accounts a ON a.id = p.primary_account_id
        WHERE p.page_id=?
    """, (page_id,)).fetchone()

    if not row:
        return {}

    return {
        "page_id":          row[0],
        "page_name":        row[1],
        "category":         row[2] or "",
        "followers":        row[3] or 0,
        "primary_account":  row[4],
        "token_updated_at": row[5] or "",
        "in_use":           bool(row[6]),
        "owner_name":       row[7] or "",
    }


def detect_token_conflicts() -> list:
    """
    Detect pages where multiple accounts have tokens.
    Returns list of conflict reports for admin review.
    """
    rows = get_db().execute("""
        SELECT
            p.page_id,
            p.page_name,
            COUNT(ap.account_id) AS token_count,
            p.primary_account_id,
            a.fb_name AS primary_name,
            p.token_updated_at
        FROM pages p
        LEFT JOIN account_pages ap ON ap.page_id = p.page_id
        LEFT JOIN fb_accounts a ON a.id = p.primary_account_id
        GROUP BY p.page_id
        HAVING COUNT(ap.account_id) > 1
        ORDER BY token_count DESC
    """).fetchall()

    return [{
        "page_id":          r[0],
        "page_name":        r[1],
        "token_count":      r[2],
        "primary_account":  r[3],
        "primary_name":     r[4] or "Unknown",
        "token_updated_at": r[5] or "",
        "status":           "multi-account",
    } for r in rows]