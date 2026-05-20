import os
import re
import json
import time
import warnings
from flask import Flask, render_template, request, jsonify, Response, redirect
from bot_worker import get_db, _db_lock, start_worker, stop_worker, worker_status
from page_monitor import start_monitor, stop_monitor, init_notifications, start_manual_followers_updater
from bot_worker import get_config as bw_get_config, set_config as bw_set_config
from bot_worker import LOCAL_UPLOADS_DIR
from fb_oauth import (
    init_oauth_tables, build_oauth_url, exchange_code_for_token,
    extend_token, get_user_info, fetch_user_pages, save_account,
    save_manual_account,
    get_all_accounts, disconnect_account, delete_account, refresh_account_pages,
    refresh_manual_token,
    get_config, set_config,
    # ✅ v10: new functions
    get_global_pages, get_page_token, validate_page_token,
    get_best_token_for_page, detect_token_conflicts, get_page_info,
    mark_page_in_use,
)
from datetime import datetime, timezone, timedelta
import ig_session as _ig_session   # ✅ v11: Instagram session manager

warnings.filterwarnings("ignore", category=DeprecationWarning)
app = Flask(__name__)

BASE_URL = os.environ.get("BASE_URL", "http://localhost:5000").rstrip("/")

PKT = timezone(timedelta(hours=-4))  # ✅ USA Eastern Time (ET/EDT, UTC-4 daylight saving)


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _now_pkt():
    return datetime.now(PKT).strftime("%Y-%m-%d %H:%M:%S ET")


# ─── Pages ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


# ─── App Config API ───────────────────────────────────────────────────────────
@app.route("/api/config", methods=["GET"])
def get_app_config():
    return jsonify({
        "app_id":     get_config("fb_app_id", ""),
        "app_secret": "****" if get_config("fb_app_secret") else "",
        "has_secret": bool(get_config("fb_app_secret")),
        "base_url":   BASE_URL,
    })


@app.route("/api/config", methods=["POST"])
def save_app_config():
    d = request.json or {}
    if d.get("app_id"):
        set_config("fb_app_id", d["app_id"].strip())
    if d.get("app_secret"):
        set_config("fb_app_secret", d["app_secret"].strip())
    return jsonify({"message": "Config save ho gaya!"})


# ─── Logo / Watermark API ─────────────────────────────────────────────────────
@app.route("/api/config/logo", methods=["GET"])
def get_logo_config():
    logo = bw_get_config("logo_path", "")
    return jsonify({
        "logo_path": logo,
        "logo_set": bool(logo and logo.strip()),
    })


@app.route("/api/config/logo", methods=["POST"])
def save_logo_config():
    d    = request.json or {}
    path = (d.get("logo_path") or "").strip()
    if path and not os.path.exists(path):
        return jsonify({"error": f"File nahi mila: {path}"}), 400
    bw_set_config("logo_path", path)
    msg = f"Logo set ho gaya: {path}" if path else "Logo hata diya gaya."
    return jsonify({"message": msg})


@app.route("/api/config/logo/upload", methods=["POST"])
def upload_logo_file():
    if "logo" not in request.files:
        return jsonify({"error": "Koi file nahi mili (field: 'logo')"}), 400
    f = request.files["logo"]
    if not f.filename:
        return jsonify({"error": "File naam khali hai"}), 400
    allowed_exts = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in allowed_exts:
        return jsonify({"error": f"Sirf image files allowed hain ({', '.join(allowed_exts)})"}), 400
    logos_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logos")
    os.makedirs(logos_dir, exist_ok=True)
    safe_name = re.sub(r'[^a-zA-Z0-9_\-.]', '_', os.path.basename(f.filename))
    save_path = os.path.join(logos_dir, safe_name)
    f.save(save_path)
    return jsonify({"path": save_path, "message": f"Logo upload ho gaya: {safe_name}"})


# ─── Facebook OAuth ───────────────────────────────────────────────────────────
@app.route("/api/oauth/url")
def oauth_url():
    app_id = get_config("fb_app_id")
    if not app_id:
        return jsonify({"error": "Pehle App ID aur Secret save karein (Settings mein)"}), 400
    redirect_uri = f"{BASE_URL}/oauth/callback"
    url = build_oauth_url(app_id, redirect_uri)
    return jsonify({"url": url})


@app.route("/oauth/callback")
def oauth_callback():
    code  = request.args.get("code")
    error = request.args.get("error_description")

    if error:
        return f"<h3>Facebook Login Error</h3><p>{error}</p><p><a href='/'>Back</a></p>"

    if not code:
        return "<h3>No code received</h3><a href='/'>Back</a>"

    app_id       = get_config("fb_app_id")
    app_secret   = get_config("fb_app_secret")
    redirect_uri = f"{BASE_URL}/oauth/callback"

    try:
        short_token = exchange_code_for_token(app_id, app_secret, redirect_uri, code)
        long_token  = extend_token(app_id, app_secret, short_token)
        user        = get_user_info(long_token)
        fb_user_id  = user.get("id")
        fb_name     = user.get("name", "")
        fb_email    = user.get("email", "")
        pages       = fetch_user_pages(long_token)
        save_account(fb_user_id, fb_name, fb_email, long_token, app_id, app_secret, pages)
        page_count  = len(pages)
        return f"""
        <html><body>
        <h3>Connected! {fb_name} — {page_count} pages mili hain.</h3>
        <p>Dashboard update ho raha hai...</p>
        <script>
        try {{
          if (window.opener && !window.opener.closed) {{
            window.opener.postMessage({{"type":"fb_connected"}}, "{BASE_URL}");
          }}
        }} catch (e) {{}}
        setTimeout(function() {{
          try {{ window.close(); }} catch (e) {{}}
          window.location.href = "/";
        }}, 1200);
        </script>
        <a href='/'>Agar popup band na ho to yahan click karo</a>
        </body></html>
        """
    except Exception as e:
        return f"<h3>Error</h3><p>{e}</p><p><a href='/'>Back</a></p>"


# ─── Accounts API ─────────────────────────────────────────────────────────────
@app.route("/api/accounts")
def get_accounts():
    try:
        return jsonify(get_all_accounts())
    except Exception:
        return jsonify([])


@app.route("/api/accounts/<int:aid>/disconnect", methods=["POST"])
def disconnect_acc(aid):
    disconnect_account(aid)
    return jsonify({"message": "Account disconnect ho gaya"})


@app.route("/api/accounts/<int:aid>/delete", methods=["POST"])
def delete_acc(aid):
    delete_account(aid)
    return jsonify({"message": "Account permanently delete ho gaya"})


@app.route("/api/accounts/<int:aid>/refresh_manual_token", methods=["POST"])
def refresh_manual_token_api(aid):
    d = request.json or {}
    page_id   = (d.get("page_id") or "").strip()
    new_token = (d.get("page_token") or "").strip()
    if not page_id or not new_token:
        return jsonify({"error": "page_id aur page_token zaroori hain"}), 400
    try:
        refresh_manual_token(aid, page_id, new_token)
        return jsonify({"message": "Token update ho gaya ✅"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/accounts/<int:aid>/refresh", methods=["POST"])
def refresh_acc(aid):
    row = get_db().execute(
        "SELECT fb_user_id FROM fb_accounts WHERE id=?", (aid,)
    ).fetchone()
    if row and (row[0] or "").startswith("manual:"):
        return jsonify({"message": "Manual account hai. Isay refresh ki zaroorat nahi."})
    try:
        count = refresh_account_pages(aid)
        return jsonify({"message": f"{count} pages refresh ho gayi"})
    except PermissionError as e:
        # ✅ FIX: FB Permission error (code 200 / OAuthException) — 500 crash nahi, proper message
        return jsonify({
            "error": "FB Permission Error",
            "detail": str(e),
            "fix": (
                "Meta for Developers → Your App → App Roles mein "
                "is user ko 'Developer' ya 'Tester' add karo. "
                "Ya app ko Live mode mein publish karo."
            )
        }), 403
    except Exception as e:
        return jsonify({"error": "Refresh fail ho gaya", "detail": str(e)}), 500


@app.route("/api/accounts/manual", methods=["POST"])
def add_manual_acc():
    d = request.json or {}
    account_name  = (d.get("account_name") or d.get("page_name") or "").strip()
    account_email = (d.get("account_email") or "").strip()
    page_name     = (d.get("page_name") or account_name).strip()
    page_id       = (d.get("page_id") or "").strip()
    page_token    = (d.get("page_token") or "").strip()
    category      = (d.get("category") or "Manual").strip()

    if not account_name:
        return jsonify({"error": "Account name zaroori hai"}), 400
    if not page_id:
        return jsonify({"error": "Page ID zaroori hai"}), 400
    if not page_token:
        return jsonify({"error": "Page token zaroori hai"}), 400

    account_id = save_manual_account(
        account_name, account_email, page_name, page_id, page_token, category
    )
    return jsonify({"id": account_id, "message": "Manual Facebook account add ho gaya!"}), 201


# ── v10 NEW: Global Pages API (deduplicated view) ─────────────────────────────
@app.route("/api/pages", methods=["GET"])
def get_pages_global():
    """
    ✅ v10: Global deduplicated pages.
    Har page sirf ek baar dikhta hai — regardless of how many accounts have it.
    """
    try:
        pages = get_global_pages()
        return jsonify(pages)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/pages/<page_id>/token_check", methods=["POST"])
def check_page_token(page_id):
    """✅ v10: Validate a page's token against Facebook API."""
    token = get_page_token(page_id)
    if not token:
        return jsonify({"valid": False, "reason": "No token found"}), 404
    result = validate_page_token(page_id, token)
    return jsonify({
        "page_id": page_id,
        "valid":   result["valid"],
        "reason":  result["reason"],
        "page_name": result.get("page_name", ""),
    })


@app.route("/api/pages/<page_id>/best_token", methods=["POST"])
def resolve_best_token(page_id):
    """
    ✅ v10: Smart token resolution.
    Tries all account tokens, promotes the valid one as primary.
    """
    token = get_best_token_for_page(page_id)
    if token:
        return jsonify({"page_id": page_id, "resolved": True, "token_preview": token[:10] + "..."})
    return jsonify({"page_id": page_id, "resolved": False, "reason": "No valid token found"}), 404


@app.route("/api/pages/conflicts", methods=["GET"])
def page_token_conflicts():
    """
    ✅ v10: Show pages that have tokens from multiple accounts.
    Admin review: helps identify and resolve token conflicts.
    """
    try:
        conflicts = detect_token_conflicts()
        return jsonify(conflicts)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/pages/<page_id>", methods=["GET"])
def get_page_detail(page_id):
    """✅ v10: Get full info for a specific page."""
    info = get_page_info(page_id)
    if not info:
        return jsonify({"error": "Page nahi mila"}), 404
    return jsonify(info)


# ── v10: Structured logs API ──────────────────────────────────────────────────
@app.route("/api/structured_logs")
def get_structured_logs():
    """✅ v10: Structured logs with request IDs, stack traces, retry events."""
    cid   = request.args.get("channel_id")
    level = request.args.get("level", "")
    limit = min(int(request.args.get("limit", 50)), 200)

    try:
        if cid and level:
            rows = get_db().execute(
                "SELECT id,channel_id,level,request_id,message,extra,ts "
                "FROM structured_logs WHERE channel_id=? AND level=? "
                "ORDER BY id DESC LIMIT ?", (cid, level.upper(), limit)
            ).fetchall()
        elif cid:
            rows = get_db().execute(
                "SELECT id,channel_id,level,request_id,message,extra,ts "
                "FROM structured_logs WHERE channel_id=? ORDER BY id DESC LIMIT ?",
                (cid, limit)
            ).fetchall()
        else:
            rows = get_db().execute(
                "SELECT id,channel_id,level,request_id,message,extra,ts "
                "FROM structured_logs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()

        result = []
        for r in rows:
            entry = {
                "id":         r[0],
                "channel_id": r[1],
                "level":      r[2],
                "request_id": r[3],
                "message":    r[4],
                "ts":         r[6],
            }
            try:
                import json as _json
                entry["extra"] = _json.loads(r[5] or "{}")
            except Exception:
                entry["extra"] = {}
            result.append(entry)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Channels API ─────────────────────────────────────────────────────────────
@app.route("/api/channels", methods=["GET"])
def get_channels():
    rows = get_db().execute(
        "SELECT id,name,source_type,source_url,tiktok_url,COALESCE(instagram_url,'') as instagram_url,photo_folder,"
        "fb_page_id,fb_token,caption_prefix,hashtags,active,"
        "daily_limit,proxy,sort_order,created_at,"
        "COALESCE(logo_path,'') as logo_path,"
        "COALESCE(logo_position,'bottom_right') as logo_position,"
        "COALESCE(logo_opacity,0.5) as logo_opacity,"
        "COALESCE(logo_scale,0.15) as logo_scale,"
        "COALESCE(google_drive_url,'') as google_drive_url,"
        "COALESCE(upload_interval_hours,0) as upload_interval_hours,"
        "COALESCE(split_duration,0) as split_duration "
        "FROM channels ORDER BY id DESC"
    ).fetchall()
    channels = []
    for r in rows:
        ch = dict(r)
        ch["status"] = worker_status(ch["id"])

        stats = get_db().execute(
            "SELECT COUNT(*), SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) "
            "FROM uploads WHERE channel_id=?", (ch["id"],)
        ).fetchone()
        ch["total_uploads"]      = stats[0] or 0
        ch["successful_uploads"] = stats[1] or 0

        ch["today_uploads"] = get_db().execute(
            "SELECT COUNT(*) FROM uploads WHERE channel_id=? AND status='success' "
            "AND DATE(uploaded_at, '-4 hours')=DATE('now', '-4 hours')", (ch["id"],)
        ).fetchone()[0]

        health = get_db().execute(
            "SELECT page_suspended,recommendations_off,growth_paused,current_followers "
            "FROM page_health WHERE channel_id=?", (ch["id"],)
        ).fetchone()
        if health:
            ch["page_suspended"]      = bool(health[0])
            ch["recommendations_off"] = bool(health[1])
            ch["growth_paused"]       = bool(health[2])
            ch["current_followers"]   = health[3] or 0
        else:
            ch["page_suspended"] = ch["recommendations_off"] = ch["growth_paused"] = False
            ch["current_followers"] = 0

        ch["has_proxy"] = bool((ch.get("proxy") or "").strip())
        ch["fb_token"]  = (ch["fb_token"] or "")[:10] + "..."
        ch["proxy"]     = ""
        # Count local uploaded videos
        local_dir = os.path.join(LOCAL_UPLOADS_DIR, str(ch["id"]))
        if os.path.isdir(local_dir):
            vexts = {".mp4",".mov",".avi",".mkv",".webm",".m4v",".3gp",".flv"}
            ch["local_file_count"] = sum(1 for f in os.listdir(local_dir) if os.path.splitext(f)[1].lower() in vexts)
        else:
            ch["local_file_count"] = 0
        channels.append(ch)
    return jsonify(channels)


@app.route("/api/channels", methods=["POST"])
def add_channel():
    d = request.json or {}
    for k in ["name","fb_page_id"]:
        if not d.get(k):
            return jsonify({"error": f"Field zaroori hai: {k}"}), 400

    page_id = (d.get("fb_page_id") or "").strip()
    raw_token = (d.get("fb_token") or "").strip()
    page_token = raw_token if raw_token and not raw_token.endswith("...") else get_page_token(page_id)
    if not page_token:
        return jsonify({
            "error": "Page token nahi mila. Account refresh karo ya page dobara connect karo."
        }), 400

    yt_url  = (d.get("source_url")       or "").strip()
    ig_url  = (d.get("instagram_url")    or "").strip()
    tk_url  = (d.get("tiktok_url")       or "").strip()
    ph_dir  = (d.get("photo_folder")     or "").strip()
    gd_url  = (d.get("google_drive_url") or "").strip()
    if not yt_url and not tk_url and not ph_dir and not ig_url and not gd_url and not d.get("use_local"):
        return jsonify({"error": "YouTube, TikTok, Instagram, Photo Folder, Google Drive ya Local Videos zaroori hai"}), 400

    daily_limit    = max(1, min(50, int(d.get("daily_limit") or 4)))
    interval_hours = max(0, min(24, int(d.get("upload_interval_hours") or 0)))
    split_dur      = max(0, min(3600, int(d.get("split_duration") or 0)))
    source_type = d.get("source_type", "youtube")
    if source_type not in ("youtube", "tiktok", "instagram", "photos", "google_drive", "local"):
        source_type = "youtube"
    sort_order = d.get("sort_order", "old_to_new")
    if sort_order not in ("old_to_new", "new_to_old", "random"):
        sort_order = "old_to_new"

    with _db_lock:
        cur = get_db().execute(
            "INSERT INTO channels "
            "(name,source_type,source_url,tiktok_url,instagram_url,photo_folder,fb_page_id,fb_token,"
            "caption_prefix,hashtags,active,daily_limit,proxy,sort_order,created_at,google_drive_url,upload_interval_hours,split_duration) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?,?,?)",
            (d["name"], source_type, yt_url, tk_url, ig_url, ph_dir,
             page_id, page_token,
             d.get("caption_prefix",""), d.get("hashtags",""),
             daily_limit, d.get("proxy",""), sort_order, _now(), gd_url, interval_hours, split_dur)
        )
        get_db().commit()
        new_id = cur.lastrowid

    init_notifications()
    start_monitor(new_id)
    return jsonify({"id": new_id, "message": "Channel add ho gaya!"}), 201


@app.route("/api/channels/<int:cid>", methods=["PATCH"])
def update_channel(cid):
    d       = request.json or {}
    allowed = ["daily_limit","caption_prefix","hashtags","name","proxy",
               "tiktok_url","instagram_url","source_url","source_type","photo_folder","sort_order",
               "logo_path","logo_position","logo_opacity","logo_scale",
               "google_drive_url","upload_interval_hours","split_duration"]
    updates = {k: d[k] for k in allowed if k in d}
    if "daily_limit" in updates:
        updates["daily_limit"] = max(1, min(50, int(updates["daily_limit"])))
    if "sort_order" in updates and updates["sort_order"] not in ("old_to_new","new_to_old","random"):
        updates["sort_order"] = "old_to_new"
    if "logo_opacity" in updates:
        updates["logo_opacity"] = max(0.1, min(1.0, float(updates["logo_opacity"])))
    if "logo_scale" in updates:
        updates["logo_scale"] = max(0.03, min(0.5, float(updates["logo_scale"])))
    if "logo_position" in updates and updates["logo_position"] not in \
            ("top_left","top_right","bottom_left","bottom_right","center","random"):
        updates["logo_position"] = "bottom_right"
    if "split_duration" in updates:
        updates["split_duration"] = max(0, min(3600, int(updates["split_duration"] or 0)))
    if not updates:
        return jsonify({"error": "Nothing to update"}), 400
    fields = ", ".join(f"{k}=?" for k in updates)
    with _db_lock:
        get_db().execute(
            f"UPDATE channels SET {fields} WHERE id=?",
            list(updates.values()) + [cid]
        )
        get_db().commit()
    return jsonify({"message": "Updated!"})


@app.route("/api/channels/<int:cid>", methods=["DELETE"])
def delete_channel(cid):
    stop_worker(cid)
    stop_monitor(cid)
    with _db_lock:
        for tbl, col in [("channels","id"),("uploads","channel_id"),("logs","channel_id"),
                         ("notifications","channel_id"),("follower_snapshots","channel_id"),
                         ("page_health","channel_id")]:
            try:
                get_db().execute(f"DELETE FROM {tbl} WHERE {col}=?", (cid,))
            except Exception:
                pass
        get_db().commit()
    return jsonify({"message": "Deleted"})


@app.route("/api/channels/<int:cid>/reset_uploads", methods=["POST"])
def reset_channel_uploads(cid):
    """Purani uploads history clear karo — channel naye source se fresh start karega."""
    with _db_lock:
        get_db().execute("DELETE FROM uploads WHERE channel_id=?", (cid,))
        get_db().commit()
    return jsonify({"message": "Uploads history reset ho gayi. Channel ab se fresh start karega."})


@app.route("/api/channels/<int:cid>/toggle", methods=["POST"])
def toggle_channel(cid):
    if worker_status(cid) == "running":
        stop_worker(cid)
        return jsonify({"status": "stopped"})

    health = get_db().execute(
        "SELECT page_suspended,growth_paused FROM page_health WHERE channel_id=?", (cid,)
    ).fetchone()
    if health:
        if health[0]:
            return jsonify({"status":"blocked","reason":"Page suspend hai."}), 400
        if health[1]:
            return jsonify({"status":"blocked","reason":"Low growth. Override use karo."}), 400

    start_worker(cid)
    return jsonify({"status": "running"})


@app.route("/api/channels/<int:cid>/force_start", methods=["POST"])
def force_start(cid):
    with _db_lock:
        get_db().execute("UPDATE page_health SET growth_paused=0 WHERE channel_id=?", (cid,))
        get_db().execute(
            "UPDATE notifications SET read=1 WHERE channel_id=? AND type='LOW_GROWTH'", (cid,)
        )
        get_db().commit()
    start_worker(cid)
    return jsonify({"status": "running"})


# ─── Bulk Start/Stop (NEW v7) ─────────────────────────────────────────────────
@app.route("/api/channels/start_all", methods=["POST"])
def start_all_channels():
    """
    ✅ FIX v7: 50 channels ek baar mein start karo — alag alag toggle ki zaroorat nahi.
    Sirf woh channels start honge jo suspend/paused nahi hain.
    """
    rows = get_db().execute("SELECT id FROM channels").fetchall()
    started = 0
    skipped = 0
    blocked = 0
    errors  = []

    for row in rows:
        cid = row[0]
        # Pehle check karo — page suspend to nahi?
        health = get_db().execute(
            "SELECT page_suspended, growth_paused FROM page_health WHERE channel_id=?", (cid,)
        ).fetchone()
        if health and health[0]:
            blocked += 1
            continue  # suspended page skip

        if worker_status(cid) == "running":
            skipped += 1
            continue  # already running

        try:
            start_worker(cid)
            started += 1
        except Exception as e:
            errors.append(f"ch{cid}: {e}")

    return jsonify({
        "message": f"{started} channels start ho gaye!",
        "started": started,
        "already_running": skipped,
        "blocked_suspended": blocked,
        "errors": errors,
    })


@app.route("/api/channels/stop_all", methods=["POST"])
def stop_all_channels():
    """
    ✅ FIX v7: 50 channels ek baar mein stop karo.
    """
    rows = get_db().execute("SELECT id FROM channels").fetchall()
    stopped = 0
    already  = 0

    for row in rows:
        cid = row[0]
        if worker_status(cid) == "running":
            stop_worker(cid)
            stopped += 1
        else:
            already += 1

    return jsonify({
        "message": f"{stopped} channels band ho gaye!",
        "stopped": stopped,
        "already_stopped": already,
    })


# ─── Uploads ──────────────────────────────────────────────────────────────────
@app.route("/api/uploads")
def get_uploads():
    cid   = request.args.get("channel_id")
    limit = int(request.args.get("limit", 20))
    if cid:
        rows = get_db().execute(
            "SELECT u.*,c.name FROM uploads u JOIN channels c ON c.id=u.channel_id "
            "WHERE u.channel_id=? ORDER BY u.id DESC LIMIT ?", (cid, limit)
        ).fetchall()
    else:
        rows = get_db().execute(
            "SELECT u.*,c.name FROM uploads u JOIN channels c ON c.id=u.channel_id "
            "ORDER BY u.id DESC LIMIT ?", (limit,)
        ).fetchall()
    cols = ["id","channel_id","video_id","title","fb_post_id","status","uploaded_at","channel_name"]
    return jsonify([dict(zip(cols,r)) for r in rows])


# ─── Notifications ────────────────────────────────────────────────────────────
@app.route("/api/notifications")
def get_notifications():
    try:
        rows = get_db().execute(
            "SELECT n.id,n.channel_id,n.type,n.title,n.message,n.severity,n.read,n.created_at,c.name "
            "FROM notifications n JOIN channels c ON c.id=n.channel_id "
            "ORDER BY n.id DESC LIMIT 50"
        ).fetchall()
    except Exception:
        return jsonify([])
    cols = ["id","channel_id","type","title","message","severity","read","created_at","channel_name"]
    return jsonify([dict(zip(cols,r)) for r in rows])


@app.route("/api/notifications/unread_count")
def unread_count():
    try:
        c = get_db().execute("SELECT COUNT(*) FROM notifications WHERE read=0").fetchone()[0]
    except Exception:
        c = 0
    return jsonify({"count": c})


@app.route("/api/notifications/<int:nid>/read", methods=["POST"])
def mark_read(nid):
    with _db_lock:
        get_db().execute("UPDATE notifications SET read=1 WHERE id=?", (nid,))
        get_db().commit()
    return jsonify({"ok": True})


@app.route("/api/notifications/read_all", methods=["POST"])
def mark_all_read():
    with _db_lock:
        get_db().execute("UPDATE notifications SET read=1")
        get_db().commit()
    return jsonify({"ok": True})


# ─── Logs SSE ─────────────────────────────────────────────────────────────────
@app.route("/api/logs/stream")
def log_stream():
    cid     = request.args.get("channel_id")
    last_id = [0]

    def generate():
        while True:
            if cid:
                rows = get_db().execute(
                    "SELECT id,level,message,ts FROM logs WHERE channel_id=? AND id>? "
                    "ORDER BY id ASC LIMIT 20", (cid, last_id[0])
                ).fetchall()
            else:
                rows = get_db().execute(
                    "SELECT id,level,message,ts FROM logs WHERE id>? "
                    "ORDER BY id ASC LIMIT 20", (last_id[0],)
                ).fetchall()
            for r in rows:
                last_id[0] = r[0]
                ts_str = r[3] or ""
                try:
                    dt_utc = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    ts_pkt = dt_utc.astimezone(PKT).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    ts_pkt = ts_str
                yield f"data: {json.dumps({'level':r[1],'message':r[2],'ts':ts_pkt})}\n\n"

            time.sleep(2)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})


# ─── Stats ────────────────────────────────────────────────────────────────────
@app.route("/api/stats")
def get_stats():
    total_ch  = get_db().execute("SELECT COUNT(*) FROM channels").fetchone()[0]
    ch_ids    = [r[0] for r in get_db().execute("SELECT id FROM channels").fetchall()]
    active_ch = sum(1 for cid in ch_ids if worker_status(cid) == "running")
    total_up  = get_db().execute("SELECT COUNT(*) FROM uploads WHERE status='success'").fetchone()[0]
    today_up  = get_db().execute(
        "SELECT COUNT(*) FROM uploads WHERE status='success' "
        "AND DATE(uploaded_at, '-4 hours')=DATE('now', '-4 hours')"
    ).fetchone()[0]
    try:
        unread_n  = get_db().execute("SELECT COUNT(*) FROM notifications WHERE read=0").fetchone()[0]
        suspended = get_db().execute("SELECT COUNT(*) FROM page_health WHERE page_suspended=1").fetchone()[0]
        accounts  = get_db().execute("SELECT COUNT(*) FROM fb_accounts WHERE active=1").fetchone()[0]
        # ✅ v10: use new pages table (globally deduplicated)
        try:
            pages = get_db().execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        except Exception:
            pages = get_db().execute("SELECT COUNT(DISTINCT page_id) FROM fb_pages").fetchone()[0]
    except Exception:
        unread_n = suspended = accounts = pages = 0

    return jsonify({
        "total_channels":       total_ch,
        "active_channels":      active_ch,
        "total_uploads":        total_up,
        "today_uploads":        today_up,
        "unread_notifications": unread_n,
        "suspended_pages":      suspended,
        "connected_accounts":   accounts,
        "total_pages":          pages,
        "server_time_pkt":      _now_pkt(),
    })


# ─── Local Video Upload API ───────────────────────────────────────────────────
ALLOWED_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".3gp", ".flv"}

@app.route("/api/channels/<int:cid>/local_files", methods=["GET"])
def list_local_files(cid):
    """Channel ki local uploaded videos list karo."""
    folder = os.path.join(LOCAL_UPLOADS_DIR, str(cid))
    if not os.path.isdir(folder):
        return jsonify([])
    files = []
    for f in sorted(os.listdir(folder)):
        ext = os.path.splitext(f)[1].lower()
        if ext not in ALLOWED_VIDEO_EXTS:
            continue
        fp = os.path.join(folder, f)
        files.append({
            "name": f,
            "size_mb": round(os.path.getsize(fp) / (1024*1024), 2),
            "added_at": datetime.fromtimestamp(os.path.getmtime(fp), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        })
    return jsonify(files)


@app.route("/api/channels/<int:cid>/local_files", methods=["POST"])
def upload_local_files(cid):
    """Local device se video files upload karo."""
    if "videos" not in request.files:
        return jsonify({"error": "Koi file nahi mili (field: 'videos')"}), 400

    folder = os.path.join(LOCAL_UPLOADS_DIR, str(cid))
    os.makedirs(folder, exist_ok=True)

    uploaded = []
    errors   = []
    files    = request.files.getlist("videos")

    for f in files:
        if not f.filename:
            continue
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in ALLOWED_VIDEO_EXTS:
            errors.append(f"{f.filename}: sirf video files allowed hain")
            continue
        safe_name = re.sub(r'[^a-zA-Z0-9_\-.]', '_', os.path.basename(f.filename))
        save_path = os.path.join(folder, safe_name)
        f.save(save_path)
        uploaded.append(safe_name)

    if not uploaded:
        return jsonify({"error": "Koi valid video file nahi mili", "details": errors}), 400

    return jsonify({
        "message": f"{len(uploaded)} video(s) upload ho gayi!",
        "uploaded": uploaded,
        "errors": errors,
    }), 201


@app.route("/api/channels/<int:cid>/local_files/<filename>", methods=["DELETE"])
def delete_local_file(cid, filename):
    """Ek local uploaded file delete karo."""
    safe_name = re.sub(r'[^a-zA-Z0-9_\-.]', '_', os.path.basename(filename))
    fpath = os.path.join(LOCAL_UPLOADS_DIR, str(cid), safe_name)
    if not os.path.exists(fpath):
        return jsonify({"error": "File nahi mili"}), 404
    os.remove(fpath)
    return jsonify({"message": f"{safe_name} delete ho gaya."})


# ─── Analytics API ───────────────────────────────────────────────────────────
@app.route("/api/analytics/uploads_per_day")
def analytics_uploads_per_day():
    rows = get_db().execute(
        "SELECT DATE(uploaded_at, '-4 hours') as day, COUNT(*) as total, "
        "SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) as success "
        "FROM uploads WHERE uploaded_at >= DATE('now','-30 days') "
        "GROUP BY day ORDER BY day ASC"
    ).fetchall()
    return jsonify([{"day": r[0], "total": r[1], "success": r[2]} for r in rows])


@app.route("/api/analytics/platform_breakdown")
def analytics_platform_breakdown():
    rows = get_db().execute(
        "SELECT COALESCE(source_platform,'youtube') as platform, "
        "COUNT(*) as total, "
        "SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) as success "
        "FROM uploads GROUP BY platform"
    ).fetchall()
    return jsonify([{"platform": r[0], "total": r[1], "success": r[2]} for r in rows])


@app.route("/api/analytics/success_rate")
def analytics_success_rate():
    total = get_db().execute("SELECT COUNT(*) FROM uploads").fetchone()[0] or 1
    succ  = get_db().execute("SELECT COUNT(*) FROM uploads WHERE status='success'").fetchone()[0]
    err   = get_db().execute("SELECT COUNT(*) FROM uploads WHERE status='error'").fetchone()[0]
    skip  = get_db().execute("SELECT COUNT(*) FROM uploads WHERE status='skipped'").fetchone()[0]
    channels = get_db().execute(
        "SELECT c.id, c.name, COUNT(u.id) as total, "
        "SUM(CASE WHEN u.status='success' THEN 1 ELSE 0 END) as success "
        "FROM channels c LEFT JOIN uploads u ON u.channel_id=c.id "
        "GROUP BY c.id ORDER BY total DESC LIMIT 10"
    ).fetchall()
    return jsonify({
        "overall": {
            "total": total, "success": succ, "error": err, "skipped": skip,
            "rate": round(succ / total * 100, 1)
        },
        "channels": [{"id": r[0], "name": r[1], "total": r[2], "success": r[3],
                      "rate": round((r[3] or 0) / max(r[2] or 1, 1) * 100, 1)} for r in channels]
    })


@app.route("/api/analytics/follower_growth")
def analytics_follower_growth():
    cid = request.args.get("channel_id")
    if cid:
        rows = get_db().execute(
            "SELECT DATE(taken_at) as day, followers FROM follower_snapshots "
            "WHERE channel_id=? ORDER BY taken_at ASC LIMIT 60", (cid,)
        ).fetchall()
    else:
        rows = get_db().execute(
            "SELECT DATE(taken_at) as day, SUM(followers) as followers "
            "FROM follower_snapshots GROUP BY day ORDER BY day ASC LIMIT 60"
        ).fetchall()
    return jsonify([{"day": r[0], "followers": r[1]} for r in rows])


@app.route("/api/analytics/token_refresh_log")
def analytics_token_log():
    rows = get_db().execute(
        "SELECT trl.channel_id, c.name, trl.refreshed_at, trl.status, trl.note "
        "FROM token_refresh_log trl LEFT JOIN channels c ON c.id=trl.channel_id "
        "ORDER BY trl.id DESC LIMIT 30"
    ).fetchall()
    return jsonify([{"channel_id": r[0], "channel_name": r[1], "refreshed_at": r[2],
                     "status": r[3], "note": r[4]} for r in rows])


# ─── Instagram Session API (v11) ─────────────────────────────────────────────

@app.route("/api/instagram/status", methods=["GET"])
def ig_session_status():
    """✅ v11: Instagram session ka current status check karo."""
    try:
        status = _ig_session.get_session_status()
        return jsonify(status)
    except Exception as e:
        return jsonify({"logged_in": False, "error": str(e)}), 500


@app.route("/api/instagram/login", methods=["POST"])
def ig_session_login():
    """
    ✅ v11: Instaloader se Instagram login karo.
    Session file + Netscape cookies.txt save ho jaati hai.
    Ek baar login = baar baar cookies banana band.

    Body: { "username": "...", "password": "..." }
    """
    d        = request.json or {}
    username = (d.get("username") or "").strip()
    password = (d.get("password") or "").strip()

    if not username:
        return jsonify({"success": False, "error": "Username zaroori hai"}), 400
    if not password:
        return jsonify({"success": False, "error": "Password zaroori hai"}), 400

    result = _ig_session.login_and_save(username, password)
    if result["success"]:
        return jsonify(result), 200
    else:
        return jsonify(result), 400


@app.route("/api/instagram/refresh", methods=["POST"])
def ig_session_refresh():
    """
    ✅ v11: Existing session se cookies refresh karo (password ki zaroorat nahi).
    Agar session expire ho gayi ho to yeh fresh cookies generate karta hai.
    """
    status = _ig_session.get_session_status()
    if not status.get("logged_in"):
        return jsonify({
            "success": False,
            "error": "Koi active session nahi. Pehle /api/instagram/login se login karein.",
        }), 400

    ok = _ig_session.load_session(status["username"])
    if ok:
        return jsonify({
            "success":  True,
            "username": status["username"],
            "message":  f"✅ @{status['username']} ki session cookies refresh ho gayi!",
        })
    else:
        return jsonify({
            "success": False,
            "error":   "Session file expire ho gayi. Dobara login karein.",
        }), 400


@app.route("/api/instagram/logout", methods=["POST"])
def ig_session_logout():
    """✅ v11: Instagram session aur cookies delete karo."""
    result = _ig_session.logout()
    return jsonify(result)


# ═══════════════════════════════════════════════════════════════════════════════
# ✅ GROUP SHARING API ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/groups", methods=["GET"])
def get_groups():
    rows = get_db().execute(
        "SELECT id, group_id, group_name, active, added_at FROM fb_groups ORDER BY id DESC"
    ).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/groups", methods=["POST"])
def add_group():
    d = request.json or {}
    group_id   = (d.get("group_id") or "").strip()
    group_name = (d.get("group_name") or group_id).strip()
    if not group_id:
        return jsonify({"error": "group_id zaroori hai"}), 400
    # URL se ID extract karo agar pura link diya
    import re
    m = re.search(r'groups/([\d]+)', group_id)
    if m:
        group_id = m.group(1)
    try:
        get_db().execute(
            "INSERT OR IGNORE INTO fb_groups(group_id,group_name,active,added_at) VALUES(?,?,1,?)",
            (group_id, group_name, _now())
        )
        get_db().commit()
        return jsonify({"ok": True, "group_id": group_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/groups/<int:gid>", methods=["DELETE"])
def delete_group(gid):
    get_db().execute("DELETE FROM fb_groups WHERE id=?", (gid,))
    get_db().commit()
    return jsonify({"ok": True})

@app.route("/api/groups/<int:gid>/toggle", methods=["POST"])
def toggle_group(gid):
    get_db().execute("UPDATE fb_groups SET active = 1 - active WHERE id=?", (gid,))
    get_db().commit()
    return jsonify({"ok": True})

@app.route("/api/groups/share_now", methods=["POST"])
def share_now():
    """Abhi foran ek sharing cycle chalao — test ke liye."""
    try:
        from bot_worker import run_group_sharing_cycle
        run_group_sharing_cycle()
        return jsonify({"ok": True, "message": "Sharing cycle complete"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/groups/history", methods=["GET"])
def group_share_history():
    rows = get_db().execute("""
        SELECT gs.*, fg.group_name
        FROM group_shares gs
        LEFT JOIN fb_groups fg ON fg.group_id = gs.group_id
        ORDER BY gs.shared_at DESC LIMIT 50
    """).fetchall()
    return jsonify([dict(r) for r in rows])

# ─── Startup ──────────────────────────────────────────────────────────────────
def _on_startup():
    init_notifications()
    try:
        init_oauth_tables()
    except Exception:
        pass
    # ✅ v7: Active channels ke workers + sab channels ke monitors restart karo
    rows = get_db().execute("SELECT id, active FROM channels").fetchall()
    monitor_count = 0
    worker_count  = 0
    for row in rows:
        cid, was_active = row[0], row[1]
        start_monitor(cid)
        monitor_count += 1
        if was_active:
            start_worker(cid)
            worker_count += 1
    if monitor_count:
        print(f"   Monitors started: {monitor_count} channels.")
    if worker_count:
        print(f"   Workers auto-restarted: {worker_count} active channels.")
    # ✅ Group sharing scheduler start karo
    from bot_worker import start_group_share_scheduler
    start_group_share_scheduler()
    print(f"   Group sharing scheduler: started")
    # ✅ Manual accounts followers auto-updater start karo
    start_manual_followers_updater()
    print(f"   Manual followers updater: started (har 6 ghante)")
    print(f"   BASE_URL: {BASE_URL}")


if __name__ == "__main__":
    print("\nYouTube / TikTok / Instagram / Photos -> Facebook Bot  v7")
    print(f"Dashboard: {BASE_URL}\n")
    _on_startup()
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)