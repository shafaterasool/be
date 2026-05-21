"""
bot_worker.py — YouTube + TikTok + Instagram + Photos → Facebook Bot
v10 PRO — DB Architecture Upgrade + Structured Logging:
  ✅ v9: All 10 upgrades (retry, log flush, disk check, rate limit backoff, etc.)
  🔥 v10 NEW:
     ✅ #11 GLOBAL PAGE SCHEMA    — pages + account_pages (no duplicates)
     ✅ #12 STRUCTURED LOGGING    — request_id, stack traces, retry logs
     ✅ #13 POSTGRESQL SUPPORT    — set DATABASE_URL env var to switch
     ✅ #14 TOKEN CONFLICT FIX    — newest valid token always wins
     ✅ #15 OWNERSHIP TRANSFER    — auto-reassign pages on account disconnect
"""

import os, re, time, random, logging, sqlite3, threading, subprocess, requests, yt_dlp, queue, shutil, glob
from structured_logger import StructuredLogger, get_logger, init_structured_logging
import ig_session as _ig_session   # ✅ v11: Instaloader session manager
from datetime import datetime, timezone, timedelta, date
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("bot_worker")

# ✅ FIX: Absolute paths — Windows pe CWD change se file-not-found bug fix
_BOT_DIR            = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS_DIR       = os.path.join(_BOT_DIR, "downloads")
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

SHORT_MAX_DURATION  = 200
LOCAL_UPLOADS_DIR   = os.path.join(_BOT_DIR, "local_uploads")  # ✅ FIX: absolute path
PKT                 = timezone(timedelta(hours=-4))  # ✅ USA Eastern Time (ET/EDT, UTC-4 daylight saving)
_DB_PATH            = "data.db"
MIN_FREE_MB         = 500          # ✅ #3 Disk: itni free space chahiye (MB)
DL_RETRY_ATTEMPTS   = 3            # ✅ #1 Retry: kitni baar try karein
DL_RETRY_BASE_DELAY = 2            # ✅ #1 Retry: base delay seconds (doubles each try)
INTER_UPLOAD_MIN    = 10           # ✅ #4 Delay: min seconds between uploads
INTER_UPLOAD_MAX    = 10           # ✅ #4 Delay: max seconds between uploads
DL_QUEUE_TIMEOUT    = 300          # ✅ #5 Queue: 300s (was 600s)

# ═══════════════════════════════════════════════════════════════════════════════
# ✅ 24/7 MODE — Upload kabhi bhi, user ki marzi se
# Peak time restriction hataa di gayi hai — bot hamesha upload karta hai
# ═══════════════════════════════════════════════════════════════════════════════

# ─── POOL ─────────────────────────────────────────────────────────────────────
_POOL         = ThreadPoolExecutor(max_workers=100, thread_name_prefix="ch")
_workers      = {}
_workers_lock = threading.Lock()

# ─── FB rate limiter ──────────────────────────────────────────────────────────
_FB_SEMAPHORE = threading.Semaphore(25)

# ✅ #8 Temp file tracker — crash pe bhi cleanup
_temp_files      = set()
_temp_files_lock = threading.Lock()

def _register_temp(path):
    with _temp_files_lock:
        _temp_files.add(path)

def _unregister_temp(path):
    with _temp_files_lock:
        _temp_files.discard(path)

def cleanup_all_temps():
    """Worker crash/stop pe call karo — saari baqi temp files hata do."""
    with _temp_files_lock:
        paths = set(_temp_files)
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.remove(p)
                log.info(f"[TempClean] Removed: {p}")
        except Exception as e:
            log.warning(f"[TempClean] Could not remove {p}: {e}")
    with _temp_files_lock:
        _temp_files.clear()

def _now():     return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
def _now_pkt(): return datetime.now(PKT).strftime("%Y-%m-%d %H:%M:%S ET")

# ─── Per-thread SQLite (WAL mode) ─────────────────────────────────────────────
_tlocal  = threading.local()
_db_lock = threading.Lock()

def get_db():
    if not getattr(_tlocal, "conn", None):
        c = sqlite3.connect(_DB_PATH, check_same_thread=False, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA busy_timeout=20000")
        c.execute("PRAGMA cache_size=-8000")
        _init_tables(c)
        _tlocal.conn = c
    return _tlocal.conn

# ✅ #6 DB Connection cleanup — thread-local conn close karo
def close_db():
    """Call at worker thread exit to free SQLite connection."""
    conn = getattr(_tlocal, "conn", None)
    if conn:
        try:
            conn.close()
        except Exception:
            pass
        _tlocal.conn = None

def _init_tables(conn):
    stmts = [
        """CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, source_type TEXT DEFAULT 'youtube',
            source_url TEXT, tiktok_url TEXT DEFAULT '',
            instagram_url TEXT DEFAULT '', photo_folder TEXT DEFAULT '',
            fb_page_id TEXT, fb_token TEXT,
            caption_prefix TEXT DEFAULT '', hashtags TEXT DEFAULT '',
            active INTEGER DEFAULT 0, daily_limit INTEGER DEFAULT 4,
            proxy TEXT DEFAULT '', sort_order TEXT DEFAULT 'old_to_new',
            created_at TEXT, logo_path TEXT DEFAULT '',
            logo_position TEXT DEFAULT 'bottom_right',
            logo_opacity REAL DEFAULT 0.5, logo_scale REAL DEFAULT 0.15)""",
        """CREATE TABLE IF NOT EXISTS uploads (
            id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id INTEGER,
            video_id TEXT, title TEXT, fb_post_id TEXT, status TEXT,
            uploaded_at TEXT, fb_page_id TEXT DEFAULT '',
            source_platform TEXT DEFAULT '',
            fb_feed_post_id TEXT DEFAULT '',
            UNIQUE(channel_id, video_id))""",
        "CREATE TABLE IF NOT EXISTS page_uploads (page_id TEXT NOT NULL, video_id TEXT NOT NULL, uploaded_at TEXT, PRIMARY KEY(page_id,video_id))",
        "CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id INTEGER, level TEXT, message TEXT, ts TEXT)",
        "CREATE TABLE IF NOT EXISTS notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id INTEGER, type TEXT, title TEXT, message TEXT, severity TEXT DEFAULT 'warning', read INTEGER DEFAULT 0, created_at TEXT)",
        "CREATE TABLE IF NOT EXISTS follower_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id INTEGER, followers INTEGER, taken_at TEXT)",
        "CREATE TABLE IF NOT EXISTS page_health (channel_id INTEGER PRIMARY KEY, page_suspended INTEGER DEFAULT 0, recommendations_off INTEGER DEFAULT 0, growth_paused INTEGER DEFAULT 0, current_followers INTEGER DEFAULT 0, last_checked TEXT)",
        "CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)",
        "CREATE TABLE IF NOT EXISTS token_refresh_log (id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id INTEGER, refreshed_at TEXT, status TEXT, note TEXT)",
        # ✅ GROUP SHARING TABLES
        "CREATE TABLE IF NOT EXISTS fb_groups (id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT UNIQUE, group_name TEXT, active INTEGER DEFAULT 1, added_at TEXT)",
        "CREATE TABLE IF NOT EXISTS group_shares (id INTEGER PRIMARY KEY AUTOINCREMENT, fb_post_id TEXT, group_id TEXT, page_id TEXT, shared_at TEXT, status TEXT DEFAULT 'pending', UNIQUE(fb_post_id, group_id))",
        """CREATE TABLE IF NOT EXISTS fb_accounts (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            fb_user_id   TEXT UNIQUE,
            fb_name      TEXT,
            fb_email     TEXT,
            user_token   TEXT,
            app_id       TEXT,
            app_secret   TEXT,
            connected_at TEXT,
            active       INTEGER DEFAULT 1)""",
        # kept for backward-compat / migration — new code uses pages + account_pages
        """CREATE TABLE IF NOT EXISTS fb_pages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER,
            page_id    TEXT,
            page_name  TEXT,
            page_token TEXT,
            category   TEXT,
            followers  INTEGER DEFAULT 0,
            in_use     INTEGER DEFAULT 0,
            UNIQUE(account_id, page_id))""",
        # ✅ v10 #11: GLOBAL pages — UNIQUE(page_id) — no duplicates ever
        """CREATE TABLE IF NOT EXISTS pages (
            page_id            TEXT PRIMARY KEY,
            page_name          TEXT DEFAULT '',
            category           TEXT DEFAULT '',
            followers          INTEGER DEFAULT 0,
            primary_account_id INTEGER,
            primary_token      TEXT DEFAULT '',
            token_updated_at   TEXT DEFAULT '',
            in_use             INTEGER DEFAULT 0)""",
        # ✅ v10 #11: account_pages — many-to-many bridge table
        """CREATE TABLE IF NOT EXISTS account_pages (
            account_id INTEGER NOT NULL,
            page_id    TEXT NOT NULL,
            page_token TEXT DEFAULT '',
            is_primary INTEGER DEFAULT 0,
            added_at   TEXT DEFAULT '',
            PRIMARY KEY (account_id, page_id))""",
        # ✅ v10 #12: structured_logs — request_id, stack traces, retry events
        """CREATE TABLE IF NOT EXISTS structured_logs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER,
            level      TEXT,
            request_id TEXT,
            message    TEXT,
            extra      TEXT,
            ts         TEXT)""",
    ]
    for s in stmts:
        conn.execute(s)
    conn.commit()
    for tbl, col, dflt in [
        ("channels","instagram_url","TEXT DEFAULT ''"),
        ("channels","tiktok_url","TEXT DEFAULT ''"),
        ("channels","photo_folder","TEXT DEFAULT ''"),
        ("channels","source_type","TEXT DEFAULT 'youtube'"),
        ("channels","daily_limit","INTEGER DEFAULT 4"),
        ("channels","proxy","TEXT DEFAULT ''"),
        ("channels","fb_page_id","TEXT DEFAULT ''"),
        ("channels","sort_order","TEXT DEFAULT 'old_to_new'"),
        ("channels","logo_path","TEXT DEFAULT ''"),
        ("channels","logo_position","TEXT DEFAULT 'bottom_right'"),
        ("channels","logo_opacity","REAL DEFAULT 0.5"),
        ("channels","logo_scale","REAL DEFAULT 0.15"),
        ("channels","google_drive_url","TEXT DEFAULT ''"),
        ("channels","upload_interval_hours","INTEGER DEFAULT 0"),
        ("channels","split_duration","INTEGER DEFAULT 0"),
        ("uploads","fb_page_id","TEXT DEFAULT ''"),
        ("uploads","source_platform","TEXT DEFAULT ''"),
        ("uploads","fb_feed_post_id","TEXT DEFAULT ''"),
        ("fb_accounts","fb_name","TEXT DEFAULT ''"),
        ("fb_accounts","fb_email","TEXT DEFAULT ''"),
        ("fb_accounts","connected_at","TEXT DEFAULT ''"),
        ("fb_pages","followers","INTEGER DEFAULT 0"),
        ("fb_pages","in_use","INTEGER DEFAULT 0"),
    ]:
        try: conn.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {dflt}"); conn.commit()
        except: pass
    try: conn.execute("ALTER TABLE channels RENAME COLUMN youtube_url TO source_url"); conn.commit()
    except: pass

# ─── Config ───────────────────────────────────────────────────────────────────
def get_config(key, default=""):
    try:
        r = get_db().execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default
    except: return default

def set_config(key, value):
    try:
        get_db().execute("INSERT INTO config(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key,value))
        get_db().commit()
    except Exception as e: log.error(f"set_config: {e}")

# ═══════════════════════════════════════════════════════════════════════════════
# ✅ #2 LOG FLUSH — DEDICATED BACKGROUND THREAD
#
# PEHLE (v8): piggyback flush — agar 10 logs buffer ho jaaye ya 5s guzar jaayein
#   Problem: worker busy ho to flush late ho sakta tha ya miss bhi
# AB   (v9): dedicated daemon thread har 3 seconds pe flush karta hai
#   Aur bhi: worker explicitly flush kar sakta hai _flush_log_buffer() se
# ═══════════════════════════════════════════════════════════════════════════════
_log_buffer      = []
_log_buffer_lock = threading.Lock()
_last_log_flush  = [time.time()]

def _flush_log_buffer():
    """Buffer ke saare pending logs ek baar mein DB mein daal do."""
    with _log_buffer_lock:
        if not _log_buffer:
            return
        buf = _log_buffer[:]
        _log_buffer.clear()
    try:
        db = get_db()
        db.executemany(
            "INSERT INTO logs(channel_id,level,message,ts) VALUES(?,?,?,?)", buf
        )
        db.commit()
    except Exception as e:
        log.error(f"log flush: {e}")
    _last_log_flush[0] = time.time()

def _log_flush_daemon():
    """✅ #2: Background thread — har 3 seconds pe log buffer flush karo."""
    while True:
        time.sleep(3)
        try:
            _flush_log_buffer()
        except Exception as e:
            log.error(f"[LogDaemon] flush error: {e}")

# Start log flush daemon once at module load
_log_flush_thread = threading.Thread(target=_log_flush_daemon, daemon=True, name="log-flusher")
_log_flush_thread.start()

def db_log(cid, level, msg):
    clean = re.sub(r'\x1b\[[0-9;]*m', '', str(msg))
    log.info(f"[ch{cid}] {clean}")
    with _log_buffer_lock:
        _log_buffer.append((cid, level, clean, _now()))
        # Still allow instant flush for critical messages
        should_flush = (len(_log_buffer) >= 20)
    if should_flush:
        _flush_log_buffer()

def add_notification(cid, ntype, title, msg, severity="warning"):
    try:
        if get_db().execute("SELECT id FROM notifications WHERE channel_id=? AND type=? AND read=0",(cid,ntype)).fetchone(): return
        get_db().execute("INSERT INTO notifications(channel_id,type,title,message,severity,created_at) VALUES(?,?,?,?,?,?)",(cid,ntype,title,msg,severity,_now()))
        get_db().commit()
    except Exception as e: log.error(f"notif: {e}")

def resolve_notification(cid, ntype):
    try:
        get_db().execute("UPDATE notifications SET read=1 WHERE channel_id=? AND type=? AND read=0",(cid,ntype))
        get_db().commit()
    except: pass

# ─── Upload tracking ──────────────────────────────────────────────────────────
def is_uploaded(cid, vid):
    return bool(get_db().execute("SELECT 1 FROM uploads WHERE channel_id=? AND video_id=?",(cid,vid)).fetchone())

def is_uploaded_page(pid, vid):
    return bool(get_db().execute("SELECT 1 FROM page_uploads WHERE page_id=? AND video_id=?",(pid,vid)).fetchone())

def get_uploaded_ids(cid, pid=""):
    """
    v8: Worker ek baar sab known IDs load karo — har video pe query nahi.
    In-memory set return karta hai → O(1) lookup instead of DB query per video.
    ✅ FIX: 'skipped' aur 'error' bhi include karo — warna failed videos
    har cycle mein dobara try hote rehte hain (infinite retry loop).
    """
    rows = get_db().execute(
        "SELECT video_id FROM uploads WHERE channel_id=? AND status IN ('success','split','skipped','error')", (cid,)
    ).fetchall()
    ids = set(r[0] for r in rows)
    if pid:
        rows2 = get_db().execute(
            "SELECT video_id FROM page_uploads WHERE page_id=?", (pid,)
        ).fetchall()
        ids.update(r[0] for r in rows2)
    return ids

def mark_uploaded(cid, vid, title, fb_page_id, fb_post_id, status="success", platform="", fb_feed_post_id=""):
    try:
        existing_feed_post_id = ""
        if not fb_feed_post_id:
            row = get_db().execute(
                "SELECT fb_feed_post_id FROM uploads WHERE channel_id=? AND video_id=?",
                (cid, vid)
            ).fetchone()
            if row:
                existing_feed_post_id = row["fb_feed_post_id"] or ""

        get_db().execute(
            "INSERT OR REPLACE INTO uploads(channel_id,video_id,title,fb_post_id,status,uploaded_at,fb_page_id,source_platform,fb_feed_post_id) VALUES(?,?,?,?,?,?,?,?,?)",
            (cid, vid, title, fb_post_id, status, _now(), fb_page_id, platform, fb_feed_post_id or existing_feed_post_id)
        )
        if status == "success" and fb_page_id:
            get_db().execute(
                "INSERT OR IGNORE INTO page_uploads(page_id,video_id,uploaded_at) VALUES(?,?,?)",
                (fb_page_id, vid, _now())
            )
        get_db().commit()
    except Exception as e: log.error(f"mark_uploaded: {e}")

def uploads_today(cid):
    # ✅ FIX v13: Count ORIGINAL videos only — not individual split parts.
    # Split parts (_p1, _p2 …) get status='success' but should not inflate
    # the daily limit.  Original split videos get status='split'.
    # Non-split uploads get status='success' with a plain video_id (no _p suffix).
    r = get_db().execute(
        "SELECT COUNT(*) FROM uploads WHERE channel_id=? "
        "AND (status='split' OR (status='success' AND video_id NOT LIKE '%\\_p%' ESCAPE '\\')) "
        "AND DATE(uploaded_at, '-4 hours')=DATE('now', '-4 hours')",
        (cid,)
    ).fetchone()
    return r[0] if r else 0

# ─── Sort ─────────────────────────────────────────────────────────────────────
def apply_sort(videos, order):
    if order == "new_to_old": return list(reversed(videos))
    if order == "random": lst=list(videos); random.shuffle(lst); return lst
    return list(videos)

# ─── Source rotation ─────────────────────────────────────────────────────────
def get_ordered_sources(ch):
    yt = (ch.get("source_url")       or "").strip()
    tk = (ch.get("tiktok_url")       or "").strip()
    ig = (ch.get("instagram_url")    or "").strip()
    ph = (ch.get("photo_folder")     or "").strip()
    gd = (ch.get("google_drive_url") or "").strip()
    # local: always check by channel id (no URL needed, folder auto-detected)
    lc = str(ch.get("id", "")) if ch.get("id") else ""
    sources = [(t,u) for t,u in [
        ("youtube",yt), ("tiktok",tk), ("instagram",ig),
        ("photos",ph),  ("google_drive",gd), ("local",lc)
    ] if u]
    if not sources: return []
    if len(sources)==1: return sources
    day   = date.today().toordinal()
    start = day % len(sources)
    return sources[start:] + sources[:start]

def get_todays_source(ch):
    s = get_ordered_sources(ch)
    return s[0] if s else ("youtube","")

# ─── VPN/Proxy ────────────────────────────────────────────────────────────────
def detect_proxy():
    for v in ["HTTPS_PROXY","HTTP_PROXY","https_proxy","http_proxy","ALL_PROXY","all_proxy"]:
        p = os.environ.get(v,"").strip()
        if p and p not in ("","null","none"): return p
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as k:
            if winreg.QueryValueEx(k,"ProxyEnable")[0]:
                s = winreg.QueryValueEx(k,"ProxyServer")[0].strip()
                if s: return ("http://" if "://" not in s else "") + s
    except: pass
    for port in [1080,8080,8118,9050,10809]:
        try:
            import socket; s=socket.socket(); s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1",port))==0: s.close(); return f"socks5://127.0.0.1:{port}"
            s.close()
        except: pass
    return ""

def _popt(proxy_str):
    p = (proxy_str or "").strip() or detect_proxy()
    if not p: return {},{}
    if not any(p.lower().startswith(s) for s in ("http://","https://","socks4://","socks5://")):
        if "." in p or "local" in p or p.startswith("127."): p = "http://"+p
        else: return {},{}
    return {"proxy":p},{"http":p,"https":p}

# ─── Watermark ────────────────────────────────────────────────────────────────
_POS = {"top_left":"20:20","top_right":"W-w-20:20","bottom_left":"20:H-h-20",
        "bottom_right":"W-w-20:H-h-20","center":"(W-w)/2:(H-h)/2"}

def apply_watermark(vp, lp, pos="bottom_right", op=0.5, sc=0.15, out_dir=None):
    if out_dir is None: out_dir = DOWNLOADS_DIR
    if not lp or not os.path.exists(lp.strip()): return vp
    op=max(0.1,min(1.0,float(op))); sc=max(0.03,min(0.5,float(sc)))
    pe = random.choice(list(_POS.values())) if pos=="random" else _POS.get(pos,_POS["bottom_right"])
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, os.path.splitext(os.path.basename(vp))[0]+"_wm.mp4")
    fc  = f"[1:v]scale=iw*{sc:.3f}:-1,format=rgba,colorchannelmixer=aa={op:.2f}[logo];[0:v][logo]overlay={pe}"
    cmd = ["ffmpeg","-y","-i",vp,"-i",lp.strip(),"-filter_complex",fc,"-c:a","copy",
           "-c:v","libx264","-preset","ultrafast","-crf","28","-movflags","+faststart",out]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=180)
        if r.returncode != 0: return vp
        if os.path.exists(vp) and vp != out:
            try: os.remove(vp)
            except: pass
        return out
    except: return vp

# ═══════════════════════════════════════════════════════════════════════════════
# ✅ RE-ENCODE — Facebook fingerprint/ContentID se bachao
#
# Kya karta hai:
#   1. Video ka hash badal deta hai (same content, alag bytes)
#   2. Metadata strip karta hai (original creator info hatao)
#   3. Minor random crop (1-3px) — visual fingerprint change
#   4. Audio pitch micro-shift — audio fingerprint change
#   5. Random start timestamp — encode time alag hoti hai
#
# Result: Facebook/YouTube ContentID same video nahi pehchan pata
# ═══════════════════════════════════════════════════════════════════════════════

def reencode_video(vp, out_dir=None):
    if out_dir is None: out_dir = DOWNLOADS_DIR
    """
    Video ko re-encode karo taake fingerprint change ho.
    Watermark ke sath ya bina dono cases mein kaam karta hai.
    """
    if not vp or not os.path.exists(vp):
        return vp

    out = os.path.join(out_dir, os.path.splitext(os.path.basename(vp))[0] + "_re.mp4")

    # Random micro-crop (1-3 px) — visual hash change karo
    crop_px = random.randint(1, 3)

    # Random CRF 23-27 — har baar alag compression = alag hash
    crf = random.randint(23, 27)

    # Audio pitch micro-shift (0.99 - 1.01) — audio fingerprint change
    pitch = round(random.uniform(0.99, 1.01), 4)

    cmd = [
        "ffmpeg", "-y",
        "-i", vp,
        "-vf", f"crop=iw-{crop_px}:ih-{crop_px}:{crop_px//2}:{crop_px//2},scale=iw:ih",  # micro crop
        "-c:v", "libx264",
        "-preset", "fast",           # ultrafast se fast — thoda better quality
        "-crf", str(crf),            # random compression
        "-c:a", "aac",
        "-af", f"atempo={pitch}",    # micro pitch shift
        "-map_metadata", "-1",       # metadata strip — original creator info hatao
        "-movflags", "+faststart",
        out
    ]

    try:
        r = subprocess.run(cmd, capture_output=True, timeout=300)
        if r.returncode == 0 and os.path.exists(out):
            # Original file hatao, re-encoded wali use karo
            try:
                if vp != out:
                    os.remove(vp)
            except Exception:
                pass
            log.debug(f"[ReEncode] ✅ Done: {os.path.basename(out)} (crf={crf}, crop={crop_px}px, pitch={pitch})")
            return out
        else:
            log.warning(f"[ReEncode] ffmpeg fail — original use kar raha hoon")
            return vp
    except Exception as e:
        log.warning(f"[ReEncode] Error: {e} — original use kar raha hoon")
        return vp

# ═══════════════════════════════════════════════════════════════════════════════
# ✅ #3 DISK SPACE CHECK
# Download se pehle dekho: kafi free space hai ya nahi
# Agar nahi to skip karo + warn karo
# ═══════════════════════════════════════════════════════════════════════════════
def check_disk_space(path=None, min_mb=MIN_FREE_MB):
    if path is None: path = DOWNLOADS_DIR
    """
    True  → enough space, proceed
    False → low disk, skip download
    """
    try:
        os.makedirs(path, exist_ok=True)
        free_mb = shutil.disk_usage(path).free / (1024 * 1024)
        if free_mb < min_mb:
            log.warning(f"[DiskCheck] ⚠️ Sirf {free_mb:.0f}MB free — min {min_mb}MB chahiye!")
            return False
        return True
    except Exception as e:
        log.warning(f"[DiskCheck] Check fail: {e} — proceed anyway")
        return True   # fail-open: disk check fail to bhi download try karo

# ═══════════════════════════════════════════════════════════════════════════════
# ✅ VIDEO SPLITTER — Equal Parts mein video split karo
#
# split_duration = seconds per part (0 = disabled)
# Example: 60s video + 10s/part = 6 equal parts
#          60s video + 12s/part = 5 equal parts
# Har part ko alag upload_id milta hai: {original_id}_p1, _p2, ...
# Original video_id status='split' se mark hota hai (daily limit mein count nahi)
# ═══════════════════════════════════════════════════════════════════════════════
import json as _json_mod
import math as _math_mod

def split_video(video_path, seconds_per_part, out_dir=None, cid=None):
    if out_dir is None: out_dir = DOWNLOADS_DIR
    """
    Video ko equal parts mein split karo using ffmpeg.
    Returns: (list of part file paths, total_parts)
    Agar split nahi hoi to ([video_path], 1) return karta hai.
    """
    os.makedirs(out_dir, exist_ok=True)

    # Get video duration via ffprobe
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", video_path],
            capture_output=True, timeout=30
        )
        info   = _json_mod.loads(probe.stdout)
        duration = float(info.get("format", {}).get("duration") or 0)
        if duration <= 0:
            # Try from streams
            for s in info.get("streams", []):
                if s.get("duration"):
                    duration = float(s["duration"]); break
    except Exception as e:
        if cid: db_log(cid, "WARN", f"[Split] Duration detect fail: {e} — split skip")
        return [video_path], 1

    if duration <= 0:
        if cid: db_log(cid, "WARN", "[Split] Duration 0 — split skip")
        return [video_path], 1

    if duration <= seconds_per_part:
        if cid: db_log(cid, "INFO",
            f"[Split] Video ({duration:.1f}s) <= part size ({seconds_per_part}s) — split ki zaroorat nahi")
        return [video_path], 1

    total_parts = _math_mod.ceil(duration / seconds_per_part)
    base_name   = os.path.splitext(os.path.basename(video_path))[0]

    if cid: db_log(cid, "INFO",
        f"✂️ [Split] Video split ho rahi hai | Duration: {duration:.1f}s | "
        f"Part size: {seconds_per_part}s | Total parts: {total_parts}")

    parts = []
    for i in range(total_parts):
        start      = i * seconds_per_part
        part_fname = f"{base_name}_part{i+1}of{total_parts}.mp4"
        part_path  = os.path.join(out_dir, part_fname)
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(start),
            "-i", video_path,
            "-t", str(seconds_per_part),
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
            "-c:a", "aac",
            "-movflags", "+faststart",
            part_path
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=300)
            if r.returncode == 0 and os.path.exists(part_path) and os.path.getsize(part_path) > 1000:
                parts.append(part_path)
                _register_temp(part_path)
                if cid: db_log(cid, "INFO", f"[Split] ✅ Part {i+1}/{total_parts} ready ({seconds_per_part}s)")
            else:
                err_msg = r.stderr.decode(errors="replace")[-200:] if r.stderr else ""
                if cid: db_log(cid, "WARN", f"[Split] ⚠️ Part {i+1}/{total_parts} fail: {err_msg}")
        except Exception as e:
            if cid: db_log(cid, "WARN", f"[Split] Part {i+1}/{total_parts} exception: {e}")

    if not parts:
        if cid: db_log(cid, "WARN", "[Split] Koi part nahi bana — original file use ho gi")
        return [video_path], 1

    if cid: db_log(cid, "INFO",
        f"[Split] ✅ {len(parts)}/{total_parts} parts ready | Upload shuru ho raha hai...")
    return parts, total_parts

# ═══════════════════════════════════════════════════════════════════════════════
# v8 INCREMENTAL FETCH — PRO LEVEL (unchanged from v8, working perfectly)
# ═══════════════════════════════════════════════════════════════════════════════
EARLY_STOP_COUNT = 8

def fetch_youtube_shorts(url, known_ids=None, max_new=5000, proxy=""):
    known  = known_ids or set()
    base   = url.rstrip("/")
    yo, _  = _popt(proxy)
    opts   = {
        "quiet": True, "no_warnings": True,
        "extract_flat": "in_playlist",
        "playlistend": 5000,
        "ignoreerrors": True,
        "socket_timeout": 30,
        **yo
    }
    # ✅ FIX v3: tv client PO Token nahi maangta — 2026 mein sabse reliable
    # ios → PO Token required=True ho gaya (fail)
    # tv_embedded → exist nahi karta new yt-dlp mein (invalid)
    # mweb → PO Token required=True ho gaya (fail)
    # tv → koi PO Token policy nahi = free mein kaam karta hai ✅
    opts["extractor_args"] = {
        "youtube": {
            "player_client": ["tv", "web", "mweb"],
        }
    }
    _cookie_file = os.path.join(_BOT_DIR, "yt_cookies.txt")
    if os.path.exists(_cookie_file) and os.path.getsize(_cookie_file) > 100:
        opts["cookiefile"] = _cookie_file
    entries = []
    for u in ([base+"/shorts", base] if "/shorts" not in base else [base]):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(u, download=False)
            if info and info.get("entries"):
                entries = [e for e in info["entries"] if e and e.get("id")]
                if entries: break
        except Exception as e: log.warning(f"YT fetch {u}: {e}")

    out               = []
    consecutive_known = 0
    for e in entries:
        d = e.get("duration") or 0
        if d > SHORT_MAX_DURATION: continue
        vid_id = e["id"]
        if vid_id in known:
            consecutive_known += 1
            if consecutive_known >= EARLY_STOP_COUNT:
                log.debug(f"YT early stop: {EARLY_STOP_COUNT} consecutive known IDs")
                break
            continue
        consecutive_known = 0
        out.append({
            "id": vid_id, "title": e.get("title",""),
            "description": e.get("description","") or "",
            "tags": e.get("tags") or [], "duration": d,
            "url": f"https://www.youtube.com/shorts/{vid_id}",
            "source": "youtube"
        })
        if len(out) >= max_new: break
    out.reverse()
    return out

def fetch_tiktok_videos(url, known_ids=None, max_new=5000, proxy=""):
    known = known_ids or set()
    yo, _ = _popt(proxy)
    opts  = {
        "quiet": True, "no_warnings": True,
        "extract_flat": "in_playlist",
        "playlistend": 5000,
        "ignoreerrors": True, "socket_timeout": 30,
        **yo
    }
    entries = []
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if info: entries = [e for e in (info.get("entries") or []) if e and e.get("id")]
    except Exception as e: log.warning(f"TikTok fetch: {e}")

    out               = []
    consecutive_known = 0
    for e in entries:
        d = e.get("duration") or 0
        if d > SHORT_MAX_DURATION: continue
        vid_id = e["id"]
        if vid_id in known:
            consecutive_known += 1
            if consecutive_known >= EARLY_STOP_COUNT: break
            continue
        consecutive_known = 0
        out.append({
            "id": vid_id, "title": e.get("title",""),
            "description": e.get("description","") or e.get("title","") or "",
            "tags": e.get("tags") or [], "duration": d,
            "url": e.get("webpage_url") or f"https://www.tiktok.com/@_/video/{vid_id}",
            "source": "tiktok"
        })
        if len(out) >= max_new: break
    out.reverse()
    return out

def fetch_instagram_reels(url, known_ids=None, max_new=100, proxy=""):

    import instaloader
    from instaloader import Profile

    known = known_ids or set()

    try:

        username = (
            url.split("?")[0]
            .rstrip("/")
            .split("/")[-1]
            .strip()
        )

        if not username:
            return []

        # LOAD INSTALOADER
        L = instaloader.Instaloader(
            quiet=True,
            download_pictures=False,
            download_videos=False,
            download_video_thumbnails=False,
            download_comments=False,
            save_metadata=False,
        )

        # ✅ FIX: Hardcoded username hata diya — ab kisi bhi account ki session load hogi
        # Step 1: ig_session_meta.json se active username lo
        # Step 2: us username ki session file load karo
        # Step 3: agar meta nahi to ig_sessions/ folder mein jo bhi pehli session mile
        _ig_meta_file = os.path.join(_BOT_DIR, "ig_session_meta.json")
        _ig_sess_dir  = os.path.join(_BOT_DIR, "ig_sessions")
        _active_user  = None

        if os.path.exists(_ig_meta_file):
            try:
                import json as _json
                with open(_ig_meta_file) as _mf:
                    _meta = _json.load(_mf)
                _active_user = _meta.get("username", "").strip()
            except Exception: pass

        # Fallback: ig_sessions/ mein pehli session file dhundo
        if not _active_user and os.path.isdir(_ig_sess_dir):
            for _fn in os.listdir(_ig_sess_dir):
                if _fn.startswith("session-"):
                    _active_user = _fn[len("session-"):]
                    break

        if not _active_user:
            log.warning("[IG] Koi active Instagram session nahi mili. "
                        "Pehle app mein Instagram login karein.")
            return []

        session_file = os.path.join(_ig_sess_dir, f"session-{_active_user}")

        if not os.path.exists(session_file):
            log.warning(f"[IG] Session file nahi mili: {session_file}")
            return []

        L.load_session_from_file(_active_user, session_file)
        log.info(f"[IG] Session load ho gayi: @{_active_user}")

        profile = Profile.from_username(
            L.context,
            username
        )

        out = []

        consecutive_known = 0

        for post in profile.get_posts():

            try:

                # ONLY VIDEO POSTS
                if not post.is_video:
                    continue

                vid = str(post.mediaid)

                full_id = "ig_" + vid

                if full_id in known:

                    consecutive_known += 1

                    if consecutive_known >= EARLY_STOP_COUNT:
                        break

                    continue

                consecutive_known = 0

                duration = (
                    getattr(post, "video_duration", 0)
                    or 0
                )

                if duration > SHORT_MAX_DURATION:
                    continue

                reel_url = (
                    f"https://www.instagram.com/reel/"
                    f"{post.shortcode}/"
                )

                caption = post.caption or ""

                out.append({
                    "id": full_id,

                    "title": (
                        caption[:80]
                        if caption else
                        "Instagram Reel"
                    ),

                    "description": caption,

                    "tags": [],

                    "duration": duration,

                    "url": reel_url,

                    "source": "instagram"
                })

                if len(out) >= max_new:
                    break

            except Exception as e:

                log.warning(
                    f"Instagram post parse fail: {e}"
                )

        out.reverse()

        log.info(
            f"Fetched {len(out)} Instagram reels "
            f"from @{username}"
        )

        return out

    except Exception as e:

        log.warning(
            f"Instagram fetch failed: {e}"
        )

        return []
    
# ─── Photos ───────────────────────────────────────────────────────────────────
IMGEXT = {".jpg",".jpeg",".png",".webp",".bmp"}

def fetch_photo_videos(folder, done_ids):
    if not os.path.isdir(folder): return []
    out = []
    for f in sorted(os.listdir(folder)):
        if os.path.splitext(f)[1].lower() not in IMGEXT: continue
        vid = "photo_" + os.path.splitext(f)[0]
        if vid in done_ids: continue
        out.append({
            "id": vid,
            "title": os.path.splitext(f)[0].replace("_"," ").replace("-"," "),
            "description": "", "tags": [], "duration": 15,
            "url": os.path.join(folder, f), "source": "photos"
        })
    return out

# ─── ✅ NEW: Local Device Uploaded Videos ─────────────────────────────────────
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".3gp", ".flv"}

def fetch_local_videos(cid, done_ids):
    """
    Local device se upload ki gayi videos padhta hai.
    Folder: ./local_uploads/<cid>/
    """
    folder = os.path.join(LOCAL_UPLOADS_DIR, str(cid))
    if not os.path.isdir(folder):
        return []
    out = []
    for f in sorted(os.listdir(folder)):
        ext = os.path.splitext(f)[1].lower()
        if ext not in VIDEO_EXTS:
            continue
        vid = "local_" + os.path.splitext(f)[0]
        if vid in done_ids:
            continue
        fpath = os.path.join(folder, f)
        out.append({
            "id": vid,
            "title": os.path.splitext(f)[0].replace("_", " ").replace("-", " "),
            "description": "", "tags": [], "duration": 60,
            "url": fpath, "source": "local",
            "_local_path": fpath,  # direct path — download step skip hoga
        })
    return out


# ─── ✅ NEW: Google Drive Support ─────────────────────────────────────────────
def fetch_google_drive_videos(gdrive_url, known_ids=None, max_new=5000, proxy=""):
    """
    Google Drive link se videos fetch karta hai (yt-dlp se).
    Single file ya folder dono support.
    """
    if known_ids is None:
        known_ids = set()
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
        "ignoreerrors": True,
    }
    if proxy:
        opts["proxy"] = proxy
    out = []
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(gdrive_url, download=False)
            if not info:
                return []
            entries = info.get("entries") or [info]
            for e in entries:
                if not e:
                    continue
                vid = e.get("id") or e.get("webpage_url", "")[-20:]
                if vid in known_ids:
                    continue
                title = e.get("title") or e.get("filename") or "Google Drive Video"
                out.append({
                    "id": "gdrive_" + vid,
                    "title": title,
                    "url": e.get("webpage_url") or e.get("url") or gdrive_url,
                    "description": e.get("description", ""),
                    "tags": [],
                    "duration": e.get("duration") or 60,
                    "source": "google_drive",
                })
                if len(out) >= max_new:
                    break
    except Exception as ex:
        log.warning(f"[GDrive] fetch error: {ex}")
    return out

def convert_photo_to_video(img, out_dir=None, dur=15):
    if out_dir is None: out_dir = DOWNLOADS_DIR
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "photo_"+os.path.splitext(os.path.basename(img))[0]+".mp4")
    cmd = ["ffmpeg","-y","-loop","1","-i",img,"-c:v","libx264","-t",str(dur),
           "-vf","scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,format=yuv420p",
           "-preset","ultrafast","-crf","28","-movflags","+faststart",out]
    r = subprocess.run(cmd, capture_output=True, timeout=60)
    if r.returncode != 0: raise RuntimeError(f"FFmpeg: {r.stderr.decode()[:200]}")
    return out

# ─── Downloads ────────────────────────────────────────────────────────────────
def _dlbase(url, tmpl, extra_opts, proxy):
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    yo, _ = _popt(proxy)
    opts  = {**{
        "quiet": True, "no_warnings": True, "merge_output_format": "mp4",
        "socket_timeout": 60, "retries": 3,
        "concurrent_fragment_downloads": 4,    # ✅ FIX: 16→4 — log spam kam, stable download
        "age_limit": 21,                        # ✅ FIX: cookies ke bina age-restricted bypass
        "outtmpl": tmpl
    }, **extra_opts, **yo}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if not info: raise ValueError("Unavailable")
        full = {
            "description": info.get("description","") or info.get("title","") or "",
            "tags": info.get("tags") or [],
            "title": info.get("title","")
        }
        path = ydl.prepare_filename(info)
        for e in [".webm",".mkv",".part"]: path = path.replace(e,".mp4")
    return _ffile(path), full

def _ffile(path):
    if os.path.exists(path): return path
    base = os.path.splitext(path)[0]
    for e in [".mp4",".webm",".mkv",".m4v"]:
        if os.path.exists(base+e): return base+e
    raise FileNotFoundError(path)

def download_youtube(url, proxy=""):
    # ✅ FIX v2: YouTube PO Token bypass — cookies ke bina bhi kaam karta hai
    # Problem: "android"+"web" clients 2025 mein YouTube PO Token maangne lage
    #          PoToken nahi = 0.1MB fake file milti hai (real video nahi)
    # Solution: "ios" + "tv_embedded" clients PO Token nahi maangaate
    #   ios         → real iPhone app request — YouTube full video deta hai
    #   tv_embedded → Smart TV client — PO token requirement nahi
    #   mweb        → Mobile browser — fallback
    extra = {
        "format": "18/22/best[ext=mp4]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best",
        "extractor_args": {
            "youtube": {
                "player_client": ["tv", "web", "mweb"],
                # ✅ FIX v3: tv client — PO Token nahi maangta, 2026 mein reliable
            },
        },
    }
    # Optional: agar cookies file ho to use karo (extra safety, zaroori nahi)
    _cookie_file = os.path.join(_BOT_DIR, "yt_cookies.txt")
    if os.path.exists(_cookie_file) and os.path.getsize(_cookie_file) > 100:
        extra["cookiefile"] = _cookie_file
    return _dlbase(url, os.path.join(DOWNLOADS_DIR, "%(id)s.%(ext)s"), extra, proxy)

def download_tiktok(url, proxy=""):
    # ✅ FIX v3: TikTok format fix
    # - h265/bytevc1 bhi accept karo (TikTok zyada tar h265 deta hai 2026 mein)
    # - app_version 35.1.3 = yt-dlp default ke saath match
    # - format preference: no-watermark h264 → h265 → best
    return _dlbase(url, os.path.join(DOWNLOADS_DIR, "%(id)s.%(ext)s"),
                   {
                       "format": (
                           "bestvideo[vcodec^=h264][ext=mp4]+bestaudio[ext=m4a]"
                           "/bestvideo[ext=mp4]+bestaudio[ext=m4a]"
                           "/mp4[vcodec^=h264]/mp4/best[ext=mp4]/best"
                       ),
                       "extractor_args": {
                           "tiktok": {
                               "api_hostname": "api16-normal-c-useast1a.tiktokv.com",
                               "app_version": "35.1.3",
                           }
                       },
                       "merge_output_format": "mp4",
                   }, proxy)

def download_instagram(url, proxy=""):
    # ✅ v11: Instaloader session cookies use karo
    extra = {"format": "best[ext=mp4]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best"}
    ig_cookies = _ig_session.get_cookies_file()
    if ig_cookies:
        extra["cookiefile"] = ig_cookies
    return _dlbase(url, os.path.join(DOWNLOADS_DIR, "ig_%(id)s.%(ext)s"), extra, proxy)

# ─── Caption ──────────────────────────────────────────────────────────────────
def build_caption(ch, vm, fi):
    parts = []
    pf = (ch.get("caption_prefix") or "").strip()
    if pf: parts.append(pf)
    desc = (fi.get("description") or vm.get("description") or "").strip()
    if desc: parts.append(desc[:4800]+("..." if len(desc)>4800 else ""))
    tags = fi.get("tags") or vm.get("tags") or []
    if tags:
        ht = " ".join("#"+re.sub(r'[^a-zA-Z0-9_]','',t.replace(" ","_")) for t in tags[:15] if t and len(t)<30)
        if ht.strip(): parts.append(ht)
    cht = (ch.get("hashtags") or "").strip()
    if cht: parts.append(cht)
    if not parts:
        t = (fi.get("title") or vm.get("title") or "").strip()
        if t: parts.append(t)
    return "\n\n".join(p for p in parts if p)

# ═══════════════════════════════════════════════════════════════════════════════
# ✅ #9 FACEBOOK UPLOAD — EXPONENTIAL BACKOFF ON RATE LIMIT
#
# PEHLE (v8): rate limit pe direct fail
# AB   (v9):  FB error codes 32/613/429 pe retry with backoff (10s→20s→40s)
#
# FB Rate Limit Codes:
#   32  → Application request limit reached
#   613 → Calls to this api have exceeded the rate limit
#   429 → Too many requests (HTTP level)
# ═══════════════════════════════════════════════════════════════════════════════
CHUNK          = 20 * 1024 * 1024
FB_RATE_CODES  = {32, 613}          # FB graph rate limit error codes
FB_RATE_WAITS  = [10, 20, 40]       # ✅ #9: backoff: 10s, 20s, 40s
FB_500_WAITS   = [15, 30, 60]       # ✅ FIX: Facebook 500 Server Error retry

def upload_to_facebook(pid, tok, fpath, title, caption, proxy=""):
    """
    ✅ REELS ONLY MODE: Har video — chahe kitni bhi lambi ho — /video_reels
       endpoint se upload hoti hai taake hamesha Reels tab mein jaaye.
    ✅ #9: Rate limit (32/613/429) pe automatic exponential backoff with retry.
    """
    fsize      = os.path.getsize(fpath)
    reels_api  = f"https://graph.facebook.com/v19.0/{pid}/video_reels"
    _, rp      = _popt(proxy); prx = rp or None

    def _do_upload():
        with _FB_SEMAPHORE:
            # ── Step 1: Initialize upload session ────────────────────────────
            init_r = requests.post(
                reels_api,
                data={
                    "access_token":    tok,
                    "upload_phase":    "start",
                    "file_size":       fsize,
                },
                timeout=30,
                proxies=prx,
            )
            init_r.raise_for_status()
            init_resp   = init_r.json()
            upload_url  = init_resp.get("upload_url") or ""
            video_id    = init_resp.get("video_id")   or ""
            if not upload_url or not video_id:
                raise ValueError(f"[Reels] Init fail — response: {init_resp}")

            # ── Step 2: Upload binary ─────────────────────────────────────────
            with open(fpath, "rb") as f:
                up_r = requests.post(
                    upload_url,
                    headers={
                        "Authorization":       f"OAuth {tok}",
                        "offset":              "0",
                        "file_size":           str(fsize),
                    },
                    data=f,
                    timeout=600,
                    proxies=prx,
                )
            up_r.raise_for_status()
            up_resp = up_r.json()
            if not up_resp.get("success"):
                raise ValueError(f"[Reels] Binary upload fail — response: {up_resp}")

            # ── Step 3: Finish / publish ──────────────────────────────────────
            fin_r = requests.post(
                reels_api,
                data={
                    "access_token":    tok,
                    "upload_phase":    "finish",
                    "video_id":        video_id,
                    "video_state":     "PUBLISHED",
                    "description":     caption[:5000],
                    "title":           title[:255],
                },
                timeout=60,
                proxies=prx,
            )
            fin_r.raise_for_status()
            fin_resp = fin_r.json()
            if not fin_resp.get("success"):
                raise ValueError(f"[Reels] Finish fail — response: {fin_resp}")

        log.info(f"[Reels] ✅ Upload complete — video_id: {video_id}")
        return video_id

    # ✅ #9: Retry loop with exponential backoff for rate limits + 500 errors
    _500_attempts = 0
    for attempt, wait in enumerate(FB_RATE_WAITS + [None], start=1):
        try:
            return _do_upload()
        except requests.HTTPError as e:
            if e.response is not None:
                if e.response.status_code == 429:
                    if wait is not None:
                        log.warning(f"[FB] HTTP 429 rate limit — {wait}s baad retry (attempt {attempt})")
                        time.sleep(wait)
                        continue
                    raise
                # ✅ FIX: Facebook 500 Server Error — random FB issue, retry karein
                if e.response.status_code == 500:
                    if _500_attempts < len(FB_500_WAITS):
                        w500 = FB_500_WAITS[_500_attempts]
                        _500_attempts += 1
                        log.warning(f"[FB] HTTP 500 Server Error — {w500}s baad retry (attempt {_500_attempts})")
                        time.sleep(w500)
                        continue
                    raise
                try: ec = e.response.json().get("error", {}).get("code", 0)
                except: ec = 0
                if ec in FB_RATE_CODES:
                    if wait is not None:
                        log.warning(f"[FB] Graph rate limit (code {ec}) — {wait}s baad retry (attempt {attempt})")
                        time.sleep(wait)
                        continue
                    raise
            raise

# ─── Token Auto-Refresh ───────────────────────────────────────────────────────
def share_reel_to_page_feed(pid, tok, reel_id, message="", proxy=""):
    """
    Reel ko page ke normal feed mein link-post ke taur par share karo
    taake public "All posts" tab khali na dikhe.
    """
    if not reel_id:
        return ""

    feed_api = f"https://graph.facebook.com/v19.0/{pid}/feed"
    reel_url = f"https://www.facebook.com/reel/{reel_id}/"
    _, rp = _popt(proxy); prx = rp or None

    def _do_share():
        with _FB_SEMAPHORE:
            r = requests.post(
                feed_api,
                data={
                    "access_token": tok,
                    "link": reel_url,
                    "message": (message or "").strip()[:5000],
                },
                timeout=60,
                proxies=prx,
            )
        r.raise_for_status()
        resp = r.json()
        post_id = resp.get("id") or ""
        if not post_id:
            raise ValueError(f"[Feed] Share fail â€” response: {resp}")
        return post_id

    for attempt, wait in enumerate(FB_RATE_WAITS + [None], start=1):
        try:
            return _do_share()
        except requests.HTTPError as e:
            if e.response is not None:
                if e.response.status_code == 429:
                    if wait is not None:
                        log.warning(f"[Feed] HTTP 429 rate limit â€” {wait}s baad retry (attempt {attempt})")
                        time.sleep(wait)
                        continue
                    raise
                try: ec = e.response.json().get("error",{}).get("code",0)
                except: ec = 0
                if ec in FB_RATE_CODES:
                    if wait is not None:
                        log.warning(f"[Feed] Graph rate limit (code {ec}) â€” {wait}s baad retry (attempt {attempt})")
                        time.sleep(wait)
                        continue
                    raise
            raise

def try_refresh_token(cid, ch):
    """
    ✅ v10 #14: Token refresh using new account_pages + pages schema.
    Falls back to fb_pages for legacy compat.
    Manual accounts: page token ko directly extend karo global app credentials se.
    """
    pid = (ch.get("fb_page_id") or "").strip()
    if not pid: return False
    logger = get_logger(channel_id=cid)
    try:
        # ── v10: Try new account_pages schema first ───────────────────────────
        row = get_db().execute(
            """SELECT fa.user_token, fa.app_id, fa.app_secret, fa.id
               FROM account_pages ap
               JOIN fb_accounts fa ON fa.id = ap.account_id
               WHERE ap.page_id=? AND fa.active=1
               ORDER BY ap.is_primary DESC LIMIT 1""",
            (pid,)
        ).fetchone()
        # Fallback to old fb_pages if account_pages has no entry
        if not row:
            row = get_db().execute(
                "SELECT fa.user_token,fa.app_id,fa.app_secret,fa.id "
                "FROM fb_pages fp JOIN fb_accounts fa ON fa.id=fp.account_id "
                "WHERE fp.page_id=? AND fa.active=1 LIMIT 1", (pid,)
            ).fetchone()
        if not row:
            db_log(cid,"WARN","[Token] Account nahi mila — manual token hai, skip.")
            return False
        ut,aid,asec,account_id = row[0],row[1],row[2],row[3]

        # ── Manual account: global app credentials se page token extend karo ──
        if not (ut and aid and asec):
            cfg_aid  = get_config("fb_app_id")  or ""
            cfg_asec = get_config("fb_app_secret") or ""
            if not (cfg_aid and cfg_asec):
                db_log(cid,"WARN","[Token] Manual account — app credentials nahi hain, auto-refresh nahi ho sakta.")
                return False
            # Current page token fetch karo
            cur_token = get_db().execute(
                "SELECT primary_token FROM pages WHERE page_id=?", (pid,)
            ).fetchone()
            if not cur_token or not cur_token[0]:
                db_log(cid,"WARN","[Token] Manual account — current token nahi mila.")
                return False
            cur_tok = cur_token[0]
            G = "https://graph.facebook.com/v19.0"
            # Page token ko directly extend karo
            r = requests.get(f"{G}/oauth/access_token",
                             params={"grant_type":"fb_exchange_token","client_id":cfg_aid,
                                     "client_secret":cfg_asec,"fb_exchange_token":cur_tok},timeout=15)
            if r.status_code != 200:
                db_log(cid,"WARN",f"[Token] Manual token extend fail: {r.text[:200]}")
                return False
            new_tok = r.json().get("access_token", "")
            if not new_tok:
                db_log(cid,"WARN","[Token] Manual token — naya token nahi mila response me.")
                return False
            # Update sab jagah
            get_db().execute("UPDATE pages SET primary_token=?, token_updated_at=? WHERE page_id=?",
                             (new_tok, _now(), pid))
            get_db().execute("UPDATE account_pages SET page_token=? WHERE page_id=? AND account_id=?",
                             (new_tok, pid, account_id))
            get_db().execute("UPDATE fb_pages SET page_token=? WHERE page_id=?", (new_tok, pid))
            get_db().execute("UPDATE channels SET fb_token=? WHERE id=?", (new_tok, cid))
            get_db().execute("INSERT INTO token_refresh_log(channel_id,refreshed_at,status,note) VALUES(?,?,?,?)",
                             (cid, _now(), "success", "Manual token auto-extended"))
            get_db().commit()
            logger.info("Manual token auto-refresh successful", page_id=pid)
            db_log(cid,"SUCCESS",f"[Token] ✅ Manual token auto-refresh kamyab! [{_now_pkt()}]")
            resolve_notification(cid,"TOKEN_EXPIRED")
            return True

        G = "https://graph.facebook.com/v19.0"
        r = requests.get(f"{G}/oauth/access_token",
                         params={"grant_type":"fb_exchange_token","client_id":aid,"client_secret":asec,"fb_exchange_token":ut},timeout=15)
        r.raise_for_status(); new_ut = r.json().get("access_token",ut)
        r2 = requests.get(f"{G}/me/accounts",
                          params={"fields":"id,name,access_token","access_token":new_ut,"limit":200},timeout=15)
        r2.raise_for_status(); pages_data = r2.json().get("data",[])
        new_pt = next((p["access_token"] for p in pages_data if p.get("id")==pid), None)
        if not new_pt:
            db_log(cid,"WARN",f"[Token] Page {pid} ka naya token nahi mila.")
            logger.warning("Token refresh: page not found in FB response", page_id=pid)
            return False
        # Update all token stores
        get_db().execute("UPDATE fb_accounts SET user_token=? WHERE id=?",(new_ut,account_id))
        # Update new schema
        get_db().execute(
            "UPDATE pages SET primary_token=?, token_updated_at=? WHERE page_id=?",
            (new_pt, _now(), pid)
        )
        get_db().execute(
            "UPDATE account_pages SET page_token=? WHERE page_id=? AND account_id=?",
            (new_pt, pid, account_id)
        )
        # Update legacy fb_pages for compat
        get_db().execute("UPDATE fb_pages SET page_token=? WHERE page_id=?",(new_pt,pid))
        get_db().execute("UPDATE channels SET fb_token=? WHERE id=?",(new_pt,cid))
        get_db().execute("INSERT INTO token_refresh_log(channel_id,refreshed_at,status,note) VALUES(?,?,?,?)",(cid,_now(),"success","60-day token renewed"))
        get_db().commit()
        logger.info("Token auto-refresh successful", page_id=pid, account_id=account_id)
        db_log(cid,"SUCCESS",f"[Token] ✅ Auto-refresh kamyab! [{_now_pkt()}]")
        resolve_notification(cid,"TOKEN_EXPIRED"); return True
    except Exception as e:
        db_log(cid,"WARN",f"[Token] Refresh fail: {e}"); return False

# ═══════════════════════════════════════════════════════════════════════════════
# ✅ #7 TOKEN PRE-VALIDATE
# Upload loop shuru karne se pehle ek baar token verify karo
# Agar invalid to turant warn karo — saari downloads waste na hoon
# ═══════════════════════════════════════════════════════════════════════════════
def validate_fb_token(pid, tok, proxy=""):
    """
    Returns (True, None) if token valid
    Returns (False, error_code) if token invalid
    """
    _, rp = _popt(proxy); prx = rp or None
    try:
        r = requests.get(
            f"https://graph.facebook.com/v19.0/{pid}",
            params={"fields": "id,name", "access_token": tok},
            timeout=10, proxies=prx
        )
        if r.status_code == 200:
            return True, None
        try:    ec = r.json().get("error",{}).get("code", 0)
        except: ec = 0
        return False, ec
    except Exception as e:
        log.warning(f"[TokenValidate] Check fail: {e}")
        return True, None   # network error → optimistic, try anyway

# ═══════════════════════════════════════════════════════════════════════════════
# ✅ #1 DOWNLOAD WITH RETRY — EXPONENTIAL BACKOFF
# ✅ #3 DISK SPACE CHECK — before each download
# ✅ #8 TEMP FILE TRACKER — register/unregister each downloaded file
# ✅ #10 DOWNLOAD PROGRESS LOG — platform + estimated file size
# ═══════════════════════════════════════════════════════════════════════════════
def _do_download(v, stype, proxy, lpath, lpos, lop, lsc, cid=None):
    """
    ✅ #3: Disk check before download
    ✅ #10: Log platform + duration before start
    ✅ #1: Retry with exponential backoff (2s→4s→8s) on network errors
    ✅ #8: Register/unregister temp files for crash cleanup
    """
    # ── #3 Disk space check ────────────────────────────────────────────────
    if not check_disk_space(DOWNLOADS_DIR, MIN_FREE_MB):
        raise RuntimeError(f"Disk space kam hai! Min {MIN_FREE_MB}MB chahiye.")

    # ── ✅ Local file: direct path — download ki zaroorat nahi ────────────
    if stype == "local" or v.get("_local_path"):
        local_path = v.get("_local_path") or v.get("url", "")
        if not os.path.exists(local_path):
            raise RuntimeError(f"Local file nahi mila: {local_path}")
        fi = {"description": v.get("description",""), "tags": v.get("tags",[]), "title": v["title"]}
        # Watermark apply karo agar logo set hai
        if lpath and os.path.exists(lpath):
            wm = apply_watermark(local_path, lpath, lpos, lop, lsc)
            if cid:
                db_log(cid,"INFO",f"💧 Watermark added: {os.path.basename(local_path)}")
            return wm, fi
        return local_path, fi  # local file directly use — cleanup nahi hoga (user ka file)

    # ── #10 Download progress log ──────────────────────────────────────────
    dur     = v.get("duration", 0)
    est_mb  = round(dur * 0.5, 1) if dur else "?"   # rough estimate ~0.5MB/sec
    plat_label = {"youtube":"YouTube","tiktok":"TikTok","instagram":"Instagram",
                  "photos":"Photos","google_drive":"Google Drive"}.get(stype, stype)
    if cid:
        db_log(cid, "INFO", f"⬇ [{plat_label}] Download shuru: '{v['title']}' | ~{dur}s | Est: ~{est_mb}MB")

    path = None
    last_err = None

    # ── #1 Retry loop ──────────────────────────────────────────────────────
    for attempt in range(1, DL_RETRY_ATTEMPTS + 1):
        try:
            if stype == "photos":
                path = convert_photo_to_video(v["url"])
                fi   = {"description": v.get("description",""), "tags": v.get("tags",[]), "title": v["title"]}
            elif stype == "tiktok":
                path, fi = download_tiktok(v["url"], proxy)
            elif stype == "instagram":
                path, fi = download_instagram(v["url"], proxy)
            else:
                path, fi = download_youtube(v["url"], proxy)

            # ── #8 Register temp file ──────────────────────────────────────
            if path:
                _register_temp(path)

            # ── Apply watermark (may produce new file) ─────────────────────
            if lpath and path:
                new_path = apply_watermark(path, lpath, lpos, lop, lsc)
                if new_path != path:
                    _unregister_temp(path)   # original replaced
                    path = new_path
                    _register_temp(path)     # register watermarked version

            # ── ✅ RE-ENCODE — fingerprint change karo (watermark ke baad) ──
            if path and os.path.exists(path):
                if cid: db_log(cid, "INFO", f"🔄 [{plat_label}] Re-encoding (fingerprint change)...")
                path = reencode_video(path, out_dir=None)
                if cid: db_log(cid, "INFO", f"✅ [{plat_label}] Re-encode complete")

            # ── #10 Log success with actual file size ──────────────────────
            if cid and path and os.path.exists(path):
                actual_mb = round(os.path.getsize(path) / (1024*1024), 1)

                # ✅ FIX: 0.1MB "downloads" = YouTube bot-block (fake/empty file)
                # Real video min ~0.5MB hoti hai — chhoti file invalid hai
                if actual_mb < 0.5:
                    db_log(cid, "WARN",
                        f"⚠️ [{plat_label}] Download invalid — sirf {actual_mb}MB mila "
                        f"(YouTube block / yt-dlp purana hai). Skip: {v.get('title','?')[:60]}")
                    try:
                        if path and os.path.exists(path): os.remove(path)
                    except Exception: pass
                    raise RuntimeError(f"INVALID_DOWNLOAD: {actual_mb}MB too small")

                db_log(cid, "INFO", f"✅ [{plat_label}] Download kamyab: {actual_mb}MB" +
                       (f" (attempt {attempt}/{DL_RETRY_ATTEMPTS})" if attempt > 1 else ""))
            return path, fi

        except Exception as e:
            last_err = e
            err_str = str(e).lower()
            # ✅ FIX: Age-restricted / Sign-in errors pe FORAN stop — retry waste hai
            if "sign in" in err_str or "age" in err_str or "inappropriate" in err_str:
                msg = f"🔞 [{plat_label}] Age-restricted video — skip kar raha hoon (retry nahi): {v.get('title','?')}"
                if cid: db_log(cid, "WARN", msg)
                else:   log.warning(msg)
                raise RuntimeError(f"AGE_RESTRICTED: {e}")
            if attempt < DL_RETRY_ATTEMPTS:
                delay = DL_RETRY_BASE_DELAY * (2 ** (attempt - 1))   # 2s, 4s, 8s
                warn  = f"⚠️ [{plat_label}] Download attempt {attempt}/{DL_RETRY_ATTEMPTS} fail — {delay}s baad retry. Error: {e}"
                if cid: db_log(cid, "WARN", warn)
                else:   log.warning(warn)
                time.sleep(delay)
            else:
                err = f"❌ [{plat_label}] Saare {DL_RETRY_ATTEMPTS} attempts fail. Last error: {e}"
                if cid: db_log(cid, "ERROR", err)
                else:   log.error(err)

    raise RuntimeError(f"Download failed after {DL_RETRY_ATTEMPTS} attempts: {last_err}")

# ═══════════════════════════════════════════════════════════════════════════════
# ─── Worker ───────────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════
# ✅ GROUP SHARING — Page ki reels Facebook groups mein auto-share
# ═══════════════════════════════════════════════════════════════════════════════

def get_active_groups():
    """Saare active groups return karo."""
    rows = get_db().execute(
        "SELECT group_id, group_name FROM fb_groups WHERE active=1"
    ).fetchall()
    return [dict(r) for r in rows]

def get_unshared_posts(limit=5):
    """
    Recent successful uploads jo kisi group mein share nahi hue.
    fb_post_id hona chahiye (unknown nahi).
    """
    rows = get_db().execute("""
        SELECT u.fb_post_id, u.fb_page_id, u.title, u.uploaded_at
        FROM uploads u
        WHERE u.status IN ('success')
          AND u.fb_post_id IS NOT NULL
          AND u.fb_post_id != ''
          AND u.fb_post_id != 'unknown'
        ORDER BY u.uploaded_at DESC
        LIMIT ?
    """, (limit,)).fetchall()
    return [dict(r) for r in rows]

def already_shared(fb_post_id, group_id):
    """Check karo ke ye post is group mein pehle share ho chuka hai."""
    row = get_db().execute(
        "SELECT id FROM group_shares WHERE fb_post_id=? AND group_id=?",
        (fb_post_id, group_id)
    ).fetchone()
    return row is not None

def mark_group_share(fb_post_id, group_id, page_id, status="done"):
    get_db().execute(
        "INSERT OR REPLACE INTO group_shares(fb_post_id,group_id,page_id,shared_at,status) VALUES(?,?,?,?,?)",
        (fb_post_id, group_id, page_id, _now(), status)
    )
    get_db().commit()

def share_post_to_group(page_token, group_id, fb_post_id, page_id, message="", proxy=""):
    """
    Ek post ko Facebook group mein share karo.
    Graph API: POST /{group-id}/feed with link
    
    Note: Ye kaam karta hai jab:
    - User token mein publish_to_groups permission ho
    - Ya page ka admin group ka bhi admin ho
    
    Safe method: link post — Facebook ki share dialog jaisi hi hai.
    """
    # Reel ka public URL banao
    post_url = f"https://www.facebook.com/reel/{fb_post_id}"
    
    _, rp = _popt(proxy)
    prx = rp or None
    
    api = f"https://graph.facebook.com/v19.0/{group_id}/feed"
    
    payload = {
        "link": post_url,
        "message": message or "Check out this video!",
        "access_token": page_token,
    }
    
    r = requests.post(api, data=payload, timeout=30, proxies=prx)
    
    if r.status_code == 200:
        return True, r.json().get("id", "")
    else:
        err = r.json().get("error", {})
        return False, err.get("message", f"HTTP {r.status_code}")

def run_group_sharing_cycle(proxy=""):
    """
    Har ghante chalega — pending posts ko groups mein share karega.
    Ek cycle mein: 1 post × saare groups
    """
    groups = get_active_groups()
    if not groups:
        log.info("[GroupShare] Koi active group nahi — skip")
        return

    posts = get_unshared_posts(limit=3)
    if not posts:
        log.info("[GroupShare] Koi naya post nahi share karne ke liye")
        return

    # Sabse naya unshared post lo
    for post in posts:
        fb_post_id = post["fb_post_id"]
        page_id    = post["fb_page_id"]
        title      = post["title"] or ""

        # Page token lo
        row = get_db().execute(
            "SELECT page_token FROM fb_pages WHERE page_id=?", (page_id,)
        ).fetchone()
        if not row:
            row = get_db().execute(
                "SELECT page_token FROM account_pages WHERE page_id=? AND page_token!='' LIMIT 1",
                (page_id,)
            ).fetchone()
        if not row or not row["page_token"]:
            log.warning(f"[GroupShare] Page token nahi mila for page {page_id}")
            continue

        tok = row["page_token"]

        shared_count = 0
        for g in groups:
            gid = g["group_id"]
            gname = g.get("group_name", gid)

            if already_shared(fb_post_id, gid):
                log.info(f"[GroupShare] Already shared: {title[:40]} → {gname}")
                continue

            ok, result = share_post_to_group(tok, gid, fb_post_id, page_id, title, proxy)

            if ok:
                mark_group_share(fb_post_id, gid, page_id, "done")
                log.info(f"[GroupShare] ✅ Shared: {title[:40]} → {gname}")
                shared_count += 1
                # Groups ke beech 30-60 sec delay — spam nahi lagta
                time.sleep(random.randint(30, 60))
            else:
                mark_group_share(fb_post_id, gid, page_id, f"error: {result}")
                log.warning(f"[GroupShare] ❌ Failed: {title[:40]} → {gname}: {result}")

        if shared_count > 0:
            # Ek post share ho gai — next cycle mein doosri
            break

# ── Group Sharing Background Thread ──────────────────────────────────────────
_group_share_stop = threading.Event()

def _group_share_loop():
    """Har 1 ghante mein ek sharing cycle."""
    log.info("[GroupShare] Scheduler shuru — har 1 ghante mein share hoga")
    while not _group_share_stop.is_set():
        try:
            run_group_sharing_cycle()
        except Exception as e:
            log.error(f"[GroupShare] Cycle error: {e}")
        # 1 ghanta wait (3600 sec) — 60sec chunks mein taake stop fast ho
        for _ in range(60):
            if _group_share_stop.is_set():
                break
            time.sleep(60)

def start_group_share_scheduler():
    t = threading.Thread(target=_group_share_loop, daemon=True, name="group-share-scheduler")
    t.start()
    log.info("[GroupShare] ✅ Group sharing scheduler started")

def stop_group_share_scheduler():
    _group_share_stop.set()

def _run_worker(cid):
    # ✅ FIX v7: stop event safely read karo — startup race condition
    se = None
    for _ in range(20):
        with _workers_lock:
            entry = _workers.get(cid)
            if entry and entry.get("stop"):
                se = entry["stop"]; break
        time.sleep(0.05)
    if se is None:
        log.error(f"[ch{cid}] stop event nahi mila — worker exit"); return

    db_log(cid,"INFO",f"Worker shuru [{_now_pkt()}]")
    last_refresh = [datetime.now(timezone.utc) - timedelta(days=8)]

    try:   # ✅ #8: Outer try — finally mein cleanup_all_temps + close_db
        while not se.is_set():
            try:
                row = get_db().execute("SELECT * FROM channels WHERE id=?", (cid,)).fetchone()
                if not row: break
                ch      = dict(row)
                dlimit  = int(ch.get("daily_limit") or 4)
                interval_h = int(ch.get("upload_interval_hours") or 0)  # ✅ NEW: interval scheduling
                done    = uploads_today(cid)
                proxy   = (ch.get("proxy") or "").strip()
                pid     = (ch.get("fb_page_id") or "").strip()
                sort    = (ch.get("sort_order") or "old_to_new").strip()
                lpath   = (ch.get("logo_path") or get_config("logo_path","")).strip()
                lpos    = ch.get("logo_position") or "bottom_right"
                lop     = float(ch.get("logo_opacity") if ch.get("logo_opacity") is not None else 0.5)
                lsc     = float(ch.get("logo_scale")   if ch.get("logo_scale")   is not None else 0.15)
                tok     = ch.get("fb_token","")

                # ── Token refresh every 7 days ─────────────────────────────────
                if (datetime.now(timezone.utc)-last_refresh[0]).days >= 7:
                    db_log(cid,"INFO","[Token] 7-day check — refresh try kar raha hoon...")
                    ok = try_refresh_token(cid,ch)
                    if ok:
                        row = get_db().execute("SELECT * FROM channels WHERE id=?", (cid,)).fetchone()
                        if row: ch = dict(row); tok = ch.get("fb_token","")
                    last_refresh[0] = datetime.now(timezone.utc)

                # ── Daily limit ────────────────────────────────────────────────
                if done >= dlimit:
                    db_log(cid,"INFO",f"Aaj ka limit ({dlimit}) poora. Kal chalega.")
                    add_notification(cid,"DAILY_LIMIT",f"Aaj ki {dlimit} videos upload!",
                                     f"'{ch['name']}' ne aaj poora kiya. Kal khud chalega.","info")
                    now_pkt = datetime.now(PKT)
                    wait    = (24-now_pkt.hour)*3600 - now_pkt.minute*60 - now_pkt.second + 60
                    se.wait(wait); resolve_notification(cid,"DAILY_LIMIT"); continue

                # ── ✅ #7 Token pre-validate before starting download pipeline ──
                if pid and tok:
                    valid, ec = validate_fb_token(pid, tok, proxy)
                    if not valid:
                        token_error_codes = {190, 102, 463, 467}
                        db_log(cid,"WARN",f"[Token] Pre-validate fail (code {ec}) — refresh try...")
                        refreshed = try_refresh_token(cid, ch)
                        if refreshed:
                            row = get_db().execute("SELECT * FROM channels WHERE id=?", (cid,)).fetchone()
                            if row: ch = dict(row); tok = ch.get("fb_token","")
                            db_log(cid,"INFO","[Token] ✅ Token refreshed — continue")
                        else:
                            add_notification(cid,"TOKEN_EXPIRED","Token Invalid!",
                                             f"'{ch['name']}' ka FB token kaam nahi. Reconnect karein.","critical")
                            db_log(cid,"ERROR","[Token] Token invalid, refresh bhi fail. 1 ghanta wait.")
                            se.wait(3600); continue

                # ── v8: Ek baar known_ids load karo — O(1) lookup ─────────────
                known_ids = get_uploaded_ids(cid, pid)
                rem       = dlimit - done

                # ── Source select with FALLBACK ────────────────────────────────
                all_sources = get_ordered_sources(ch)
                if not all_sources:
                    db_log(cid,"WARN","Koi source set nahi."); se.wait(3600); continue

                new_vs = []
                stype  = "youtube"
                surl   = ""

                for _stype, _surl in all_sources:
                    _src_label = {"youtube":"YouTube","tiktok":"TikTok",
                                  "instagram":"Instagram","photos":"Photos",
                                  "local":"Local Videos","google_drive":"Google Drive"}.get(_stype,_stype)
                    db_log(cid,"INFO",f"📡 Source try: [{_src_label}] | Sort: {sort}")

                    if _stype == "photos":
                        _videos = fetch_photo_videos(_surl, known_ids)
                    elif _stype == "local":
                        _videos = fetch_local_videos(cid, known_ids)
                    elif _stype == "google_drive":
                        _videos = fetch_google_drive_videos(_surl, known_ids=known_ids, max_new=rem*3, proxy=proxy)
                    elif _stype == "tiktok":
                        _videos = fetch_tiktok_videos(_surl, known_ids=known_ids, max_new=rem*3, proxy=proxy)
                    elif _stype == "instagram":
                        _videos = fetch_instagram_reels(_surl, known_ids=known_ids, max_new=rem*3, proxy=proxy)
                    else:
                        _videos = fetch_youtube_shorts(_surl, known_ids=known_ids, max_new=rem*3, proxy=proxy)

                    if _videos:
                        _videos = apply_sort(_videos, sort)
                        _new_vs = [v for v in _videos if v["id"] not in known_ids] if _stype not in ("photos","local") else _videos
                        if _new_vs:
                            new_vs=_new_vs; stype=_stype; surl=_surl
                            db_log(cid,"INFO",f"[{_src_label}] {len(_new_vs)} nai videos mili")
                            break
                        else:
                            db_log(cid,"INFO",f"[{_src_label}] Saari videos pehle se upload — next source try")
                    else:
                        db_log(cid,"WARN",f"[{_src_label}] Koi video nahi mili — next source try")

                src_label = {"youtube":"YouTube","tiktok":"TikTok",
                             "instagram":"Instagram","photos":"Photos",
                             "local":"Local Videos","google_drive":"Google Drive"}.get(stype,stype)

                if not new_vs:
                    db_log(cid,"INFO","Tamam sources khaali")
                    if len(all_sources) > 1:
                        add_notification(cid,"CHANNEL_EXHAUSTED","Saari videos ho gayi!",
                                         f"'{ch['name']}' ka content khatam. Naya source add karein.","warning")
                        break
                    else:
                        add_notification(cid,"NO_VIDEOS","Koi videos nahi mili",
                                         f"'{ch['name']}' [{src_label}] — koi item nahi.","warning")
                        se.wait(3600); continue

                resolve_notification(cid,"NO_VIDEOS"); resolve_notification(cid,"CHANNEL_EXHAUSTED")
                db_log(cid,"INFO",f"Aaj {done}/{dlimit} — {rem} aur. [{src_label}] Nai: {len(new_vs)}")

                # ══════════════════════════════════════════════════════════════
                # ✅ v7 PIPELINE + v9 improvements
                # ══════════════════════════════════════════════════════════════
                _dl_stop = threading.Event()
                _dl_q    = queue.Queue(maxsize=6)
                # ✅ FIX v13: Interval mode — 1 full original video per cycle.
                # Without this fix, split parts consumed the entire daily limit
                # in a single cycle, so the 2nd-hour upload never happened.
                if interval_h > 0:
                    _target    = new_vs[:1]       # 1 video per interval cycle
                    part_limit = float("inf")     # never cut parts mid-video
                else:
                    _target    = new_vs[:rem]
                    part_limit = rem

                def _downloader():
                    for _v in _target:
                        if se.is_set() or _dl_stop.is_set(): break
                        # Note: db_log called inside _do_download (#10)
                        try:
                            # ✅ #1 + #3 + #8 + #10: All in _do_download
                            _path, _fi = _do_download(_v, stype, proxy, lpath, lpos, lop, lsc, cid=cid)
                            _dl_q.put(("ok", _v, _path, _fi))
                        except Exception as _e:
                            _err_s = str(_e)
                            # ✅ FIX: Age-restricted — alag status taake log clear ho
                            if "AGE_RESTRICTED" in _err_s:
                                db_log(cid, "WARN", f"🔞 Age-blocked, permanent skip: {_v['title']}")
                                _dl_q.put(("age_blocked", _v, None, _err_s))
                            else:
                                db_log(cid, "WARN", f"Download fail (all retries): {_v['title']} — {_e}")
                                _dl_q.put(("err", _v, None, _err_s))
                    _dl_q.put(None)  # sentinel

                dl_thread = threading.Thread(target=_downloader, daemon=True, name=f"dl-ch{cid}")
                dl_thread.start()

                done_now = 0
                split_dur = int(ch.get("split_duration") or 0)  # ✅ NEW: video split duration

                while not se.is_set() and done_now < part_limit:
                    try:
                        # ✅ #5: 300s timeout (was 600s)
                        item = _dl_q.get(timeout=DL_QUEUE_TIMEOUT)
                    except queue.Empty:
                        db_log(cid,"WARN",f"Download queue {DL_QUEUE_TIMEOUT}s timeout — agle round mein try")
                        break

                    if item is None: break
                    status, v, path, fi_or_err = item

                    if status == "err":
                        mark_uploaded(cid,v["id"],v["title"],pid,"",status="skipped",platform=stype)
                        known_ids.add(v["id"])
                        continue

                    # ✅ FIX: Age-restricted video — permanent skip, dobara try nahi
                    if status == "age_blocked":
                        mark_uploaded(cid,v["id"],v["title"],pid,"",status="skipped",platform=stype)
                        known_ids.add(v["id"])
                        db_log(cid,"WARN",f"🔞 Permanent skip (age-restricted): {v['title']}")
                        continue

                    fi      = fi_or_err
                    caption = build_caption(ch, v, fi)

                    # ═══════════════════════════════════════════════════════════
                    # ✅ VIDEO SPLITTING — agar split_duration set hai to split karo
                    # ═══════════════════════════════════════════════════════════
                    if split_dur > 0 and path and os.path.exists(path) and stype != "local":
                        part_paths, total_parts = split_video(path, split_dur, DOWNLOADS_DIR, cid=cid)
                    else:
                        part_paths  = [path]
                        total_parts = 1

                    is_split    = (total_parts > 1)
                    orig_title  = fi.get("title") or v["title"]

                    if is_split:
                        db_log(cid, "INFO",
                            f"📊 [{orig_title}] Kitne parts: {total_parts} | "
                            f"Uploaded: 0 | Baki: {total_parts}")

                    uploaded_parts_count = 0

                    try:
                        for part_idx, part_path in enumerate(part_paths, 1):
                            if se.is_set() or done_now >= part_limit:
                                break

                            # Unique ID for each part
                            if is_split:
                                part_vid_id = f"{v['id']}_p{part_idx}"
                                title       = f"{orig_title} (Part {part_idx}/{total_parts})"
                            else:
                                part_vid_id = v["id"]
                                title       = orig_title

                            # Skip already-uploaded parts
                            if part_vid_id in known_ids:
                                if is_split:
                                    db_log(cid, "INFO",
                                        f"[Split] Part {part_idx}/{total_parts} pehle upload ho chuki — skip")
                                    uploaded_parts_count += 1
                                continue

                            try:
                                # ✅ #9: Rate limit backoff inside upload_to_facebook
                                fbid = upload_to_facebook(pid, tok, part_path, title, caption, proxy)
                                feed_post_id = ""
                                try:
                                    feed_post_id = share_reel_to_page_feed(pid, tok, fbid, caption, proxy)
                                except Exception as feed_err:
                                    db_log(cid, "WARN", f"[Feed] Reel upload ho gayi lekin page post nahi bani: {feed_err}")
                                mark_uploaded(cid, part_vid_id, title, pid, fbid, platform=stype, fb_feed_post_id=feed_post_id)
                                known_ids.add(part_vid_id)
                                done_now += 1
                                uploaded_parts_count += 1

                                if is_split:
                                    remaining_parts = total_parts - part_idx
                                    db_log(cid, "SUCCESS",
                                        f"✅ Part {part_idx}/{total_parts} upload ho gayi: {title} | "
                                        f"Total: {total_parts} | Uploaded: {uploaded_parts_count} | "
                                        f"Baki: {remaining_parts} [{_now_pkt()}]")
                                else:
                                    db_log(cid, "SUCCESS",
                                        f"✅ {done_now}/{rem} uploaded: {title} [{_now_pkt()}]")

                                # ✅ FIX v13: Interval + Split mode — har part ke baad interval wait
                                more_parts = (part_idx < total_parts) and not se.is_set()
                                more_vids  = (done_now < part_limit) and not se.is_set()

                                if interval_h > 0 and is_split and more_parts:
                                    # Har part ke baad poora interval wait — user ki request
                                    wait_secs = interval_h * 3600
                                    next_part  = part_idx + 1
                                    db_log(cid, "INFO",
                                        f"⏰ [Split+Interval] Part {part_idx}/{total_parts} done. "
                                        f"Part {next_part} {interval_h} ghante baad [{_now_pkt()}]")
                                    add_notification(cid, "INTERVAL_WAIT",
                                        f"Next part {interval_h}h mein",
                                        f"'{ch['name']}': Part {part_idx}/{total_parts} upload. "
                                        f"Part {next_part} {interval_h} ghante mein.", "info")
                                    se.wait(wait_secs)
                                    resolve_notification(cid, "INTERVAL_WAIT")
                                elif more_parts or more_vids:
                                    # ✅ Human-like gap — 3-8 min random delay
                                    delay = 20
                                    next_t = (datetime.now(PKT) + timedelta(seconds=delay)).strftime("%I:%M %p")
                                    db_log(cid, "INFO",
                                        f"⏳ Next upload: {next_t} ET ({delay} sec baad)")
                                    se.wait(delay)

                            except requests.HTTPError as e:
                                if e.response is not None:
                                    try: ec = e.response.json().get("error",{}).get("code",0)
                                    except: ec = 0
                                    if ec in (190,102,463,467):
                                        db_log(cid,"WARN",f"[Token] Expire (code {ec}) — refresh try...")
                                        if try_refresh_token(cid,ch):
                                            nr = get_db().execute("SELECT fb_token FROM channels WHERE id=?", (cid,)).fetchone()
                                            if nr: ch["fb_token"] = nr["fb_token"]; tok = ch["fb_token"]
                                            try:
                                                fbid = upload_to_facebook(pid,tok,part_path,title,caption,proxy)
                                                feed_post_id = ""
                                                try:
                                                    feed_post_id = share_reel_to_page_feed(pid, tok, fbid, caption, proxy)
                                                except Exception as feed_err:
                                                    db_log(cid,"WARN",f"[Feed] Retry upload ho gayi lekin page post nahi bani: {feed_err}")
                                                mark_uploaded(cid,part_vid_id,title,pid,fbid,platform=stype,fb_feed_post_id=feed_post_id)
                                                known_ids.add(part_vid_id)
                                                done_now += 1
                                                uploaded_parts_count += 1
                                                db_log(cid,"SUCCESS",f"✅ Retry success: {title}")
                                                # Interval+Split: retry ke baad bhi wait
                                                _rmore = (part_idx < total_parts) and not se.is_set()
                                                if interval_h > 0 and is_split and _rmore:
                                                    _rnext = part_idx + 1
                                                    db_log(cid,"INFO",
                                                        f"⏰ [Split+Interval] Retry part {part_idx}/{total_parts} done. "
                                                        f"Part {_rnext} {interval_h}h baad.")
                                                    se.wait(interval_h * 3600)
                                                continue
                                            except Exception as e2:
                                                db_log(cid,"ERROR",f"Retry fail: {e2}")
                                        else:
                                            add_notification(cid,"TOKEN_EXPIRED","Token Expire!",
                                                             f"'{ch['name']}' ka FB token kaam nahi. Reconnect karein.","critical")
                                db_log(cid,"ERROR",f"FB upload fail (Part {part_idx}/{total_parts}): {e}")
                                mark_uploaded(cid,part_vid_id,title,pid,"",status="error",platform=stype)

                            except Exception as e:
                                db_log(cid,"ERROR",f"Upload fail (Part {part_idx}/{total_parts}): {e}")
                                mark_uploaded(cid,part_vid_id,title,pid,"",status="error",platform=stype)

                            finally:
                                # Cleanup individual part file (not the original)
                                if is_split and part_path != path and part_path and os.path.exists(part_path):
                                    _unregister_temp(part_path)
                                    try: os.remove(part_path)
                                    except: pass

                        # After all parts done: mark original video as 'split' so it's not re-fetched
                        if is_split:
                            all_done = all(f"{v['id']}_p{n}" in known_ids for n in range(1, total_parts+1))
                            db_log(cid, "INFO",
                                f"[Split] ✅ Video complete | Total: {total_parts} | "
                                f"Uploaded: {uploaded_parts_count} | "
                                f"Baki: {total_parts - uploaded_parts_count}")
                            # Mark original to prevent re-download (status='split', not counted in daily limit)
                            mark_uploaded(cid, v["id"], orig_title, pid, "", status="split", platform=stype)
                            known_ids.add(v["id"])

                    finally:
                        # ✅ #8: Unregister + delete original temp file after all parts done
                        # Local files delete nahi karo — user ka data hai
                        if path and os.path.exists(path) and stype != "local" and not v.get("_local_path"):
                            _unregister_temp(path)
                            try: os.remove(path)
                            except: pass

                # Downloader stop
                _dl_stop.set()
                dl_thread.join(timeout=15)
                _flush_log_buffer()

                total = uploads_today(cid)

                # ✅ NEW: Interval scheduling mode
                if interval_h > 0:
                    # Har N ghante mein 1 video — interval mode
                    wait_secs = interval_h * 3600
                    db_log(cid,"INFO",f"⏰ Interval mode: {interval_h} ghante baad agli upload [{_now_pkt()}]")
                    add_notification(cid,"INTERVAL_WAIT",f"Next upload {interval_h}h mein",
                                     f"'{ch['name']}': {total}/{dlimit} aaj. Agli upload {interval_h} ghante mein.","info")
                    se.wait(wait_secs)
                    resolve_notification(cid,"INTERVAL_WAIT")
                elif total >= dlimit:
                    db_log(cid,"INFO",f"Aaj mukammal! {total} videos [{_now_pkt()}]")
                    add_notification(cid,"DAILY_LIMIT",f"Aaj ki {total} videos upload!",
                                     f"'{ch['name']}' ne aaj kaam poora kiya. Kal khud chalega.","info")
                    now_pkt = datetime.now(PKT)
                    wait    = (24-now_pkt.hour)*3600 - now_pkt.minute*60 - now_pkt.second + 60
                    se.wait(wait); resolve_notification(cid,"DAILY_LIMIT")
                else:
                    wait_time = 60 if (dlimit - total) > 0 else 300
                    se.wait(wait_time)

            except Exception as e:
                db_log(cid,"ERROR",f"Worker error: {e}"); se.wait(60)

    finally:
        # ✅ #8: Crash/stop pe saari temp files clean karo
        cleanup_all_temps()
        # ✅ #6: Thread-local DB connection close karo
        close_db()

    _flush_log_buffer()
    db_log(cid,"INFO","Worker band ho gaya.")
    _flush_log_buffer()
    # Use a fresh connection for final update since we closed the thread-local one
    try:
        conn = sqlite3.connect(_DB_PATH, timeout=10)
        conn.execute("UPDATE channels SET active=0 WHERE id=?", (cid,))
        conn.commit()
        conn.close()
    except Exception as e:
        log.error(f"[ch{cid}] Final active=0 update fail: {e}")

# ─── Worker management ────────────────────────────────────────────────────────
def start_worker(cid):
    """✅ FIX v7: entry pehle set, phir submit — race condition fix."""
    with _workers_lock:
        if cid in _workers and not _workers[cid]["future"].done():
            return False
        se = threading.Event()
        _workers[cid] = {"future": None, "stop": se}
        fut = _POOL.submit(_run_worker, cid)
        _workers[cid]["future"] = fut
    get_db().execute("UPDATE channels SET active=1 WHERE id=?", (cid,))
    get_db().commit()
    return True

def stop_worker(cid):
    with _workers_lock:
        if cid in _workers:
            _workers[cid]["stop"].set()
            return True
    return False

def worker_status(cid):
    with _workers_lock:
        if cid not in _workers: return "stopped"
        return "stopped" if _workers[cid]["future"] is None or _workers[cid]["future"].done() else "running"
