"""
page_monitor.py — Facebook page health monitor
v3.1 Fix: executescript() commit error fixed for Python 3.12/3.14
"""

import time
import threading
import logging
import requests
from datetime import datetime, timezone, timedelta

from bot_worker import get_db, _db_lock, stop_worker, db_log, worker_status

log = logging.getLogger("page_monitor")
_monitor_threads = {}

GROWTH_THRESHOLD   = 500
GROWTH_WINDOW_DAYS = 30


def _now():
    return datetime.now(timezone.utc).isoformat()


def init_notifications():
    """
    FIXED: Use individual execute() instead of executescript().
    executescript() in Python 3.12+ auto-commits and leaves connection
    in a broken state where subsequent commit() calls fail.
    """
    db = get_db()
    db.execute("""CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel_id INTEGER, type TEXT, title TEXT,
        message TEXT, severity TEXT DEFAULT 'warning',
        read INTEGER DEFAULT 0, created_at TEXT
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS follower_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel_id INTEGER, followers INTEGER, taken_at TEXT
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS page_health (
        channel_id INTEGER PRIMARY KEY,
        page_suspended INTEGER DEFAULT 0,
        recommendations_off INTEGER DEFAULT 0,
        growth_paused INTEGER DEFAULT 0,
        current_followers INTEGER DEFAULT 0,
        last_checked TEXT
    )""")
    try:
        db.commit()
    except Exception:
        pass  # already committed is fine


def add_notification(channel_id, ntype, title, message, severity="warning"):
    try:
        if get_db().execute(
            "SELECT id FROM notifications WHERE channel_id=? AND type=? AND read=0",
            (channel_id, ntype)
        ).fetchone():
            return
        with _db_lock:
            get_db().execute(
                "INSERT INTO notifications (channel_id,type,title,message,severity,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (channel_id, ntype, title, message, severity, _now())
            )
            get_db().commit()
    except Exception as e:
        log.error(f"add_notification error: {e}")


def resolve_notification(channel_id, ntype):
    try:
        with _db_lock:
            get_db().execute(
                "UPDATE notifications SET read=1 WHERE channel_id=? AND type=? AND read=0",
                (channel_id, ntype)
            )
            get_db().commit()
    except Exception:
        pass


def update_health(channel_id, **kwargs):
    kwargs["last_checked"] = _now()
    try:
        with _db_lock:
            get_db().execute(
                "INSERT INTO page_health (channel_id) VALUES (?) ON CONFLICT(channel_id) DO NOTHING",
                (channel_id,)
            )
            fields = ", ".join(f"{k}=?" for k in kwargs)
            get_db().execute(
                f"UPDATE page_health SET {fields} WHERE channel_id=?",
                list(kwargs.values()) + [channel_id]
            )
            get_db().commit()
    except Exception as e:
        log.error(f"update_health error: {e}")


def check_page_status(page_id, token, proxy_str=""):
    result = {"suspended": False, "followers": 0, "error": None}
    proxies = None
    if proxy_str and proxy_str.strip():
        p = proxy_str.strip()
        proxies = {"http": p, "https": p}

    try:
        r = requests.get(
            f"https://graph.facebook.com/v19.0/{page_id}",
            params={"fields": "fan_count,followers_count,name", "access_token": token},
            timeout=15, proxies=proxies
        )
        if r.status_code != 200:
            err  = r.json().get("error", {})
            code = err.get("code", 0)
            if code in (190, 102, 463, 467):
                result["error"] = f"Token expire ho gaya (code {code}). Naya token lo."
            elif code in (368, 200, 32):
                result["suspended"] = True
            else:
                result["error"] = f"FB API {code}: {err.get('message','')}"
            return result

        data = r.json()
        result["followers"] = data.get("followers_count") or data.get("fan_count") or 0

    except Exception as e:
        result["error"] = str(e)

    return result


def check_follower_growth(channel_id, current_followers):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=GROWTH_WINDOW_DAYS)).isoformat()
    try:
        with _db_lock:
            get_db().execute(
                "INSERT INTO follower_snapshots (channel_id,followers,taken_at) VALUES (?,?,?)",
                (channel_id, current_followers, _now())
            )
            get_db().commit()
    except Exception:
        pass

    old = get_db().execute(
        "SELECT followers,taken_at FROM follower_snapshots "
        "WHERE channel_id=? AND taken_at>=? ORDER BY taken_at ASC LIMIT 1",
        (channel_id, cutoff)
    ).fetchone()

    if not old:
        return False, 0, 0

    old_f, old_d = old[0], old[1]
    try:
        old_dt = datetime.fromisoformat(old_d.replace("Z", "+00:00"))
        days   = (datetime.now(timezone.utc) - old_dt).days
    except Exception:
        days = 0

    gained = current_followers - old_f
    if days >= GROWTH_WINDOW_DAYS and gained < GROWTH_THRESHOLD:
        return True, gained, days
    return False, gained, days


def _monitor_loop(channel_id, interval=21600):
    stop_event = _monitor_threads[channel_id]["stop"]

    # FIXED: db_log now has proper error handling — no crash on commit
    db_log(channel_id, "INFO", "Health monitor shuru.")

    while not stop_event.is_set():
        try:
            row = get_db().execute(
                "SELECT id, name, fb_page_id, fb_token, proxy FROM channels WHERE id=?",
                (channel_id,)
            ).fetchone()
            if not row:
                break

            # sqlite3.Row → named access
            ch_id     = row["id"]
            ch_name   = row["name"]
            page_id   = row["fb_page_id"]
            token     = row["fb_token"]
            proxy_str = row["proxy"] or ""

            db_log(channel_id, "INFO", f"Page health check: '{ch_name}'")

            status = check_page_status(page_id, token, proxy_str)

            if status["error"] and not status["suspended"]:
                db_log(channel_id, "WARN", f"Health error: {status['error']}")
                if "Token expire" in (status["error"] or ""):
                    add_notification(channel_id, "TOKEN_EXPIRED",
                        "Facebook Token Expire Ho Gaya",
                        f"'{ch_name}' ka token kaam nahi kar raha. Reconnect karein.",
                        severity="critical")
                stop_event.wait(interval)
                continue

            if status["suspended"]:
                add_notification(channel_id, "PAGE_SUSPENDED",
                    "Page Suspend Ho Gaya!",
                    f"'{ch_name}' ka page suspend hai. Facebook appeal karein.",
                    severity="critical")
                if worker_status(channel_id) == "running":
                    stop_worker(channel_id)
                update_health(channel_id, page_suspended=1)
            else:
                resolve_notification(channel_id, "PAGE_SUSPENDED")
                resolve_notification(channel_id, "TOKEN_EXPIRED")
                update_health(channel_id, page_suspended=0)

            followers = status["followers"]
            update_health(channel_id, current_followers=followers)

            should_pause, gained, days = check_follower_growth(channel_id, followers)
            if should_pause:
                add_notification(channel_id, "LOW_GROWTH",
                    "Page Grow Nahi Kar Raha",
                    f"'{ch_name}': {days} din mein sirf {gained} followers. Upload paused.",
                    severity="warning")
                if worker_status(channel_id) == "running":
                    stop_worker(channel_id)
                update_health(channel_id, growth_paused=1)
            else:
                if days >= GROWTH_WINDOW_DAYS:
                    resolve_notification(channel_id, "LOW_GROWTH")
                    update_health(channel_id, growth_paused=0)
                db_log(channel_id, "INFO",
                       f"Page OK. Followers: {followers:,} (+{gained} in {days}d)")

        except Exception as e:
            db_log(channel_id, "ERROR", f"Monitor error: {e}")

        stop_event.wait(interval)

    db_log(channel_id, "INFO", "Health monitor band.")


def start_monitor(channel_id, interval=21600):
    init_notifications()
    if channel_id in _monitor_threads and _monitor_threads[channel_id]["thread"].is_alive():
        return False
    stop = threading.Event()
    t = threading.Thread(target=_monitor_loop, args=(channel_id, interval), daemon=True)
    _monitor_threads[channel_id] = {"thread": t, "stop": stop}
    t.start()
    return True


def stop_monitor(channel_id):
    if channel_id in _monitor_threads:
        _monitor_threads[channel_id]["stop"].set()
        return True
    return False


# ══════════════════════════════════════════════════════════════════════════════
# ✅ Manual Accounts Followers Auto-Update (har 6 ghante)
# ══════════════════════════════════════════════════════════════════════════════
_manual_followers_thread = None
_manual_followers_stop   = threading.Event()

def _manual_followers_loop(interval=21600):
    """Manual accounts ke pages ke followers har 6 ghante me update karo."""
    while not _manual_followers_stop.wait(interval):
        try:
            db = get_db()
            manual_pages = db.execute("""
                SELECT ap.page_id, ap.page_token
                FROM account_pages ap
                JOIN fb_accounts fa ON fa.id = ap.account_id
                WHERE fa.fb_user_id LIKE 'manual:%' AND fa.active=1
                  AND ap.page_token IS NOT NULL AND ap.page_token != ''
            """).fetchall()

            updated = 0
            for row in manual_pages:
                page_id, token = row[0], row[1]
                try:
                    r = requests.get(
                        f"https://graph.facebook.com/v19.0/{page_id}",
                        params={"fields": "fan_count,followers_count", "access_token": token},
                        timeout=15
                    )
                    if r.status_code == 200:
                        data = r.json()
                        followers = data.get("followers_count") or data.get("fan_count") or 0
                        if followers > 0:
                            with _db_lock:
                                get_db().execute(
                                    "UPDATE pages SET followers=? WHERE page_id=?",
                                    (followers, page_id)
                                )
                                get_db().commit()
                            updated += 1
                except Exception:
                    pass

            if updated:
                log.info(f"Manual accounts: {updated} pages ke followers update ho gaye")
        except Exception as e:
            log.error(f"Manual followers update error: {e}")


def start_manual_followers_updater():
    global _manual_followers_thread
    _manual_followers_stop.clear()
    # Startup pe ek baar turant run karo
    t0 = threading.Thread(target=_manual_followers_loop, args=(21600,), daemon=True)
    _manual_followers_thread = t0
    t0.start()
    return True


def stop_manual_followers_updater():
    _manual_followers_stop.set()