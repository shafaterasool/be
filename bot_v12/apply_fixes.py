"""
apply_fixes.py — Bot v12 Auto Patch
Chalane ka tareeqa:
    python apply_fixes.py

Ye script bot_worker.py aur ig_session.py ko automatically fix kar dega.
Pehle backup bana dega, phir fixes apply karega.
"""

import os, sys, shutil, ast
from datetime import datetime

# ─── Config ───────────────────────────────────────────────────────────────────
SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
BW_FILE      = os.path.join(SCRIPT_DIR, "bot_worker.py")
IG_FILE      = os.path.join(SCRIPT_DIR, "ig_session.py")
BACKUP_STAMP = datetime.now().strftime("%Y%m%d_%H%M%S")

OK  = "✅"
ERR = "❌"
INF = "🔧"

def backup(path):
    bak = path + f".bak_{BACKUP_STAMP}"
    shutil.copy2(path, bak)
    print(f"   💾 Backup: {os.path.basename(bak)}")
    return bak

def check_syntax(path):
    with open(path, encoding="utf-8") as f:
        src = f.read()
    try:
        ast.parse(src)
        return True
    except SyntaxError as e:
        print(f"   {ERR} Syntax error: {e}")
        return False

def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()

def write(path, content):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

def patch(content, old, new, label):
    if old in content:
        print(f"   {OK} {label}")
        return content.replace(old, new)
    else:
        print(f"   ⚠️  Skip (already patched?): {label}")
        return content

# ══════════════════════════════════════════════════════════════════════════════
# bot_worker.py FIXES
# ══════════════════════════════════════════════════════════════════════════════
def fix_bot_worker():
    if not os.path.exists(BW_FILE):
        print(f"{ERR} bot_worker.py nahi mila: {BW_FILE}")
        return False

    print(f"\n{INF} bot_worker.py fix ho raha hai...")
    backup(BW_FILE)
    c = read(BW_FILE)

    # ── Fix 1: Absolute DOWNLOADS_DIR ────────────────────────────────────────
    c = patch(c,
        'log = logging.getLogger("bot_worker")\n\nSHORT_MAX_DURATION  = 200\nLOCAL_UPLOADS_DIR   = "./local_uploads"',
        'log = logging.getLogger("bot_worker")\n\n'
        '# FIX: Absolute paths — Windows pe CWD change se file-not-found bug fix\n'
        '_BOT_DIR            = os.path.dirname(os.path.abspath(__file__))\n'
        'DOWNLOADS_DIR       = os.path.join(_BOT_DIR, "downloads")\n'
        'os.makedirs(DOWNLOADS_DIR, exist_ok=True)\n\n'
        'SHORT_MAX_DURATION  = 200\n'
        'LOCAL_UPLOADS_DIR   = os.path.join(_BOT_DIR, "local_uploads")',
        "Fix 1: Absolute DOWNLOADS_DIR"
    )

    # ── Fix 2: FB 500 retry constant ─────────────────────────────────────────
    c = patch(c,
        'FB_RATE_CODES  = {32, 613}          # FB graph rate limit error codes\n'
        'FB_RATE_WAITS  = [10, 20, 40]       # ✅ #9: backoff: 10s, 20s, 40s',
        'FB_RATE_CODES  = {32, 613}          # FB graph rate limit error codes\n'
        'FB_RATE_WAITS  = [10, 20, 40]       # ✅ #9: backoff: 10s, 20s, 40s\n'
        'FB_500_WAITS   = [15, 30, 60]       # FIX: Facebook 500 Server Error retry',
        "Fix 2: FB 500 retry constant"
    )

    # ── Fix 3: FB 500 retry logic in upload_to_facebook ──────────────────────
    c = patch(c,
        '    # ✅ #9: Retry loop with exponential backoff for rate limits\n'
        '    for attempt, wait in enumerate(FB_RATE_WAITS + [None], start=1):\n'
        '        try:\n'
        '            return _do_upload()\n'
        '        except requests.HTTPError as e:\n'
        '            if e.response is not None:\n'
        '                if e.response.status_code == 429:\n'
        '                    if wait is not None:\n'
        '                        log.warning(f"[FB] HTTP 429 rate limit — {wait}s baad retry (attempt {attempt})")\n'
        '                        time.sleep(wait)\n'
        '                        continue\n'
        '                    raise\n'
        '                try: ec = e.response.json().get("error", {}).get("code", 0)\n'
        '                except: ec = 0\n'
        '                if ec in FB_RATE_CODES:\n'
        '                    if wait is not None:\n'
        '                        log.warning(f"[FB] Graph rate limit (code {ec}) — {wait}s baad retry (attempt {attempt})")\n'
        '                        time.sleep(wait)\n'
        '                        continue\n'
        '                    raise\n'
        '            raise',
        '    # FIX: Retry loop — rate limits + FB 500 Server Error\n'
        '    _500_attempts = 0\n'
        '    for attempt, wait in enumerate(FB_RATE_WAITS + [None], start=1):\n'
        '        try:\n'
        '            return _do_upload()\n'
        '        except requests.HTTPError as e:\n'
        '            if e.response is not None:\n'
        '                if e.response.status_code == 429:\n'
        '                    if wait is not None:\n'
        '                        log.warning(f"[FB] HTTP 429 — {wait}s baad retry (attempt {attempt})")\n'
        '                        time.sleep(wait)\n'
        '                        continue\n'
        '                    raise\n'
        '                # FIX: Facebook 500 Server Error — random FB glitch, retry\n'
        '                if e.response.status_code == 500:\n'
        '                    if _500_attempts < len(FB_500_WAITS):\n'
        '                        w500 = FB_500_WAITS[_500_attempts]\n'
        '                        _500_attempts += 1\n'
        '                        log.warning(f"[FB] HTTP 500 — {w500}s baad retry (attempt {_500_attempts})")\n'
        '                        time.sleep(w500)\n'
        '                        continue\n'
        '                    raise\n'
        '                try: ec = e.response.json().get("error", {}).get("code", 0)\n'
        '                except: ec = 0\n'
        '                if ec in FB_RATE_CODES:\n'
        '                    if wait is not None:\n'
        '                        log.warning(f"[FB] Graph rate limit (code {ec}) — {wait}s baad retry (attempt {attempt})")\n'
        '                        time.sleep(wait)\n'
        '                        continue\n'
        '                    raise\n'
        '            raise',
        "Fix 3: FB 500 retry logic"
    )

    # ── Fix 4: ./downloads → DOWNLOADS_DIR ───────────────────────────────────
    replacements = [
        ('out_dir="./downloads")',    'out_dir=None)'),
        ('path="./downloads"',        'path=None'),
        ('os.makedirs("./downloads", exist_ok=True)', 'os.makedirs(DOWNLOADS_DIR, exist_ok=True)'),
        ('check_disk_space("./downloads"',            'check_disk_space(DOWNLOADS_DIR'),
        ('split_video(path, split_dur, "./downloads"','split_video(path, split_dur, DOWNLOADS_DIR'),
        ('"./downloads/%(id)s.%(ext)s"',              'os.path.join(DOWNLOADS_DIR, "%(id)s.%(ext)s")'),
        ('"./downloads/ig_%(id)s.%(ext)s"',           'os.path.join(DOWNLOADS_DIR, "ig_%(id)s.%(ext)s")'),
    ]
    changed = 0
    for old, new in replacements:
        if old in c:
            c = c.replace(old, new)
            changed += 1
    if changed:
        print(f"   {OK} Fix 4: ./downloads → DOWNLOADS_DIR ({changed} replacements)")
    else:
        print(f"   ⚠️  Skip (already patched?): Fix 4: ./downloads paths")

    # ── Fix 4b: Function bodies default None → DOWNLOADS_DIR ─────────────────
    for fn_sig, insert_line in [
        ('def apply_watermark(vp, lp, pos="bottom_right", op=0.5, sc=0.15, out_dir=None):',
         '    if out_dir is None: out_dir = DOWNLOADS_DIR'),
        ('def reencode_video(vp, out_dir=None):',
         '    if out_dir is None: out_dir = DOWNLOADS_DIR'),
        ('def split_video(video_path, seconds_per_part, out_dir=None',
         None),
        ('def convert_photo_to_video(img, out_dir=None',
         None),
        ('def check_disk_space(path=None',
         None),
    ]:
        if fn_sig in c and insert_line and insert_line not in c:
            c = c.replace(fn_sig, fn_sig + "\n" + insert_line)

    # Fix split/convert/check disk None defaults
    for fn_sig, line in [
        ('def split_video(video_path, seconds_per_part, out_dir=None, cid=None):\n    os.makedirs',
         'def split_video(video_path, seconds_per_part, out_dir=None, cid=None):\n    if out_dir is None: out_dir = DOWNLOADS_DIR\n    os.makedirs'),
        ('def convert_photo_to_video(img, out_dir=None, dur=15):\n    os.makedirs',
         'def convert_photo_to_video(img, out_dir=None, dur=15):\n    if out_dir is None: out_dir = DOWNLOADS_DIR\n    os.makedirs'),
        ('def check_disk_space(path=None, min_mb=MIN_FREE_MB):\n',
         'def check_disk_space(path=None, min_mb=MIN_FREE_MB):\n    if path is None: path = DOWNLOADS_DIR\n'),
    ]:
        if fn_sig in c and 'if out_dir is None' not in c.split(fn_sig)[1][:100] if 'out_dir' in fn_sig else 'if path is None' not in c.split(fn_sig)[1][:100]:
            c = c.replace(fn_sig, line)

    print(f"   {OK} Fix 4b: Function None defaults")

    # ── Fix 5: YouTube PO Token — ios/tv_embedded clients ────────────────────
    c = patch(c,
        '"youtube": {"player_client": ["android", "web"]},',
        '"youtube": {\n'
        '                "player_client": ["ios", "tv_embedded", "mweb"],\n'
        '                "player_skip": ["webpage"],\n'
        '            },',
        "Fix 5: YouTube ios/tv_embedded client (PO Token bypass)"
    )

    # ── Fix 6: fetch_youtube_shorts — extractor_args add ─────────────────────
    c = patch(c,
        '    # ✅ FIX: Age-restricted channels ke liye cookies load karo\n'
        '    _cookie_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "yt_cookies.txt")\n'
        '    if os.path.exists(_cookie_file) and os.path.getsize(_cookie_file) > 100:\n'
        '        opts["cookiefile"] = _cookie_file',
        '    # FIX: PO Token bypass for fetch — ios/tv_embedded clients\n'
        '    opts["extractor_args"] = {\n'
        '        "youtube": {\n'
        '            "player_client": ["ios", "tv_embedded", "mweb"],\n'
        '            "player_skip": ["webpage"],\n'
        '        }\n'
        '    }\n'
        '    _cookie_file = os.path.join(_BOT_DIR, "yt_cookies.txt")\n'
        '    if os.path.exists(_cookie_file) and os.path.getsize(_cookie_file) > 100:\n'
        '        opts["cookiefile"] = _cookie_file',
        "Fix 6: fetch_youtube_shorts extractor_args"
    )

    # ── Fix 7: Hardcoded Instagram username ──────────────────────────────────
    c = patch(c,
        '        # LOAD SESSION\n'
        '        session_file = os.path.join(\n'
        '            "ig_sessions",\n'
        '            f"session-shrs0402"\n'
        '        )\n\n'
        '        L.load_session_from_file(\n'
        '            "shrs0402",\n'
        '            session_file\n'
        '        )',
        '        # FIX: Hardcoded username hata diya — kisi bhi account ki session load hogi\n'
        '        _ig_meta_file = os.path.join(_BOT_DIR, "ig_session_meta.json")\n'
        '        _ig_sess_dir  = os.path.join(_BOT_DIR, "ig_sessions")\n'
        '        _active_user  = None\n'
        '        if os.path.exists(_ig_meta_file):\n'
        '            try:\n'
        '                import json as _json\n'
        '                with open(_ig_meta_file) as _mf:\n'
        '                    _meta = _json.load(_mf)\n'
        '                _active_user = _meta.get("username", "").strip()\n'
        '            except Exception: pass\n'
        '        if not _active_user and os.path.isdir(_ig_sess_dir):\n'
        '            for _fn in os.listdir(_ig_sess_dir):\n'
        '                if _fn.startswith("session-"):\n'
        '                    _active_user = _fn[len("session-"):]\n'
        '                    break\n'
        '        if not _active_user:\n'
        '            log.warning("[IG] Koi active session nahi. App mein Instagram login karein.")\n'
        '            return []\n'
        '        session_file = os.path.join(_ig_sess_dir, f"session-{_active_user}")\n'
        '        if not os.path.exists(session_file):\n'
        '            log.warning(f"[IG] Session file nahi mili: {session_file}")\n'
        '            return []\n'
        '        L.load_session_from_file(_active_user, session_file)\n'
        '        log.info(f"[IG] Session load: @{_active_user}")',
        "Fix 7: Instagram hardcoded username"
    )

    write(BW_FILE, c)

    if check_syntax(BW_FILE):
        print(f"   {OK} bot_worker.py syntax check passed")
        return True
    else:
        print(f"   {ERR} Syntax error! Backup se restore kar raha hoon...")
        shutil.copy2(BW_FILE + f".bak_{BACKUP_STAMP}", BW_FILE)
        return False


# ══════════════════════════════════════════════════════════════════════════════
# ig_session.py FIXES
# ══════════════════════════════════════════════════════════════════════════════
def fix_ig_session():
    if not os.path.exists(IG_FILE):
        print(f"\n⚠️  ig_session.py nahi mila — skip")
        return True

    print(f"\n{INF} ig_session.py fix ho raha hai...")
    backup(IG_FILE)
    c = read(IG_FILE)

    c = patch(c,
        'SESSION_DIR       = "./ig_sessions"\n'
        'COOKIES_FILE      = "./ig_session_cookies.txt"\n'
        'SESSION_META_FILE = "./ig_session_meta.json"',
        '# FIX: Absolute paths\n'
        '_IG_BOT_DIR       = os.path.dirname(os.path.abspath(__file__))\n'
        'SESSION_DIR       = os.path.join(_IG_BOT_DIR, "ig_sessions")\n'
        'COOKIES_FILE      = os.path.join(_IG_BOT_DIR, "ig_session_cookies.txt")\n'
        'SESSION_META_FILE = os.path.join(_IG_BOT_DIR, "ig_session_meta.json")',
        "Fix: Absolute paths"
    )

    c = patch(c,
        '    if os.path.exists("instagram_cookies.txt"):\n'
        '        return "instagram_cookies.txt"',
        '    _alt = os.path.join(_IG_BOT_DIR, "instagram_cookies.txt")\n'
        '    if os.path.exists(_alt):\n'
        '        return _alt',
        "Fix: Absolute fallback cookies path"
    )

    write(IG_FILE, c)

    if check_syntax(IG_FILE):
        print(f"   {OK} ig_session.py syntax check passed")
        return True
    else:
        print(f"   {ERR} Syntax error! Backup se restore kar raha hoon...")
        shutil.copy2(IG_FILE + f".bak_{BACKUP_STAMP}", IG_FILE)
        return False


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 55)
    print("  Bot v12 — Auto Patch Script")
    print("=" * 55)

    bw_ok = fix_bot_worker()
    ig_ok = fix_ig_session()

    print("\n" + "=" * 55)
    if bw_ok and ig_ok:
        print(f"{OK} Saari fixes apply ho gayi!")
        print(f"\nAb ye karo:")
        print(f"   pip install -U yt-dlp")
        print(f"   Bot restart karo")
    else:
        print(f"{ERR} Kuch fixes fail hui. Backup files check karo (.bak_...)")
    print("=" * 55)
