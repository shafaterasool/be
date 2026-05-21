# ─────────────────────────────────────────────────────────────
# INSTAGRAM SESSION MANAGER (UPDATED FIXED VERSION)
# ─────────────────────────────────────────────────────────────

import os
import re
import time
import json
import logging
import argparse
import http.cookiejar

log = logging.getLogger("ig_session")

# ✅ FIX: Absolute paths — Windows pe CWD change se file-not-found bug fix
_IG_BOT_DIR       = os.path.dirname(os.path.abspath(__file__))
SESSION_DIR       = os.path.join(_IG_BOT_DIR, "ig_sessions")
COOKIES_FILE      = os.path.join(_IG_BOT_DIR, "ig_session_cookies.txt")
SESSION_META_FILE = os.path.join(_IG_BOT_DIR, "ig_session_meta.json")


# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────

def _ensure_dir():
    os.makedirs(SESSION_DIR, exist_ok=True)


def _session_file_path(username: str) -> str:
    return os.path.join(SESSION_DIR, f"session-{username}")


def _clean_username(username: str) -> str:
    """
    Email diya ho to username extract karo
    """
    username = username.strip()

    if "@" in username:
        username = username.split("@")[0]

    return username


# ─────────────────────────────────────────────────────────────
# LOGIN & SAVE SESSION
# ─────────────────────────────────────────────────────────────

def login_and_save(username: str, password: str, twofa_code: str = "") -> dict:

    _ensure_dir()

    try:
        import instaloader

        username = _clean_username(username)

        L = instaloader.Instaloader(
            quiet=True,
            download_pictures=False,
            download_videos=False,
            download_video_thumbnails=False,
            download_comments=False,
            save_metadata=False,
            dirname_pattern=SESSION_DIR,
        )

        log.info(f"[IG] @{username} ko login kar raha hoon...")

        try:
            # NORMAL LOGIN
            L.login(username, password)

        except instaloader.exceptions.TwoFactorAuthRequiredException:

            log.info("[IG] 2FA required")

            # CLI mode input
            if not twofa_code:

                print("\n📱 Instagram 2FA code enter karo")
                twofa_code = input("2FA Code: ").strip()

            if not twofa_code:
                return {
                    "success": False,
                    "error": "2FA code required"
                }

            # Clean code
            twofa_code = re.sub(r"\D", "", twofa_code)

            try:
                L.two_factor_login(twofa_code)

            except Exception as e:
                return {
                    "success": False,
                    "error": f"2FA login failed: {e}"
                }

        # SAVE SESSION
        sf = _session_file_path(username)

        L.save_session_to_file(sf)

        log.info(f"[IG] Session save ho gayi: {sf}")

        # EXPORT COOKIES
        _export_cookies(L, COOKIES_FILE)

        meta = {
            "username": username,
            "logged_in": True,
            "session_file": sf,
            "cookies_file": COOKIES_FILE,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

        with open(SESSION_META_FILE, "w") as f:
            json.dump(meta, f, indent=2)

        return {
            "success": True,
            "username": username,
            "message": f"✅ @{username} login successful"
        }

    except Exception as e:

        err = str(e)

        if "bad_password" in err.lower():
            msg = "❌ Wrong password"

        elif "checkpoint" in err.lower():
            msg = (
                "❌ Instagram security checkpoint.\n"
                "Instagram app me manually login karo "
                "aur suspicious activity verify karo."
            )

        elif "429" in err or "ratelimit" in err.lower():
            msg = "❌ Instagram rate limit. 15-30 minute baad try karo."

        else:
            msg = f"❌ Login fail: {err}"

        log.error(f"[IG] Login fail (@{username}): {err}")

        return {
            "success": False,
            "error": msg
        }


# ─────────────────────────────────────────────────────────────
# LOAD SESSION
# ─────────────────────────────────────────────────────────────

def load_session(username: str) -> bool:

    _ensure_dir()

    username = _clean_username(username)

    sf = _session_file_path(username)

    if not os.path.exists(sf):
        log.warning(f"[IG] Session file missing: {sf}")
        return False

    try:
        import instaloader

        L = instaloader.Instaloader(quiet=True)

        L.load_session_from_file(username, sf)

        # REFRESH COOKIES
        _export_cookies(L, COOKIES_FILE)

        log.info(f"[IG] Session load ho gayi: @{username}")

        return True

    except Exception as e:

        log.warning(f"[IG] Session load fail: {e}")

        return False


# ─────────────────────────────────────────────────────────────
# EXPORT COOKIES
# ─────────────────────────────────────────────────────────────

def _export_cookies(L, output_file: str):

    try:
        session = L.context._session

    except AttributeError:
        session = getattr(L.context, "session", None)

    if session is None:
        raise RuntimeError("Instagram session missing")

    cj = http.cookiejar.MozillaCookieJar(output_file)

    now = int(time.time())

    far_future = now + 365 * 24 * 3600

    for cookie in session.cookies:

        expires = getattr(cookie, "expires", None) or far_future

        domain = cookie.domain or ".instagram.com"

        c = http.cookiejar.Cookie(
            version=0,
            name=cookie.name,
            value=cookie.value or "",
            port=None,
            port_specified=False,
            domain=domain,
            domain_specified=True,
            domain_initial_dot=domain.startswith("."),
            path=cookie.path or "/",
            path_specified=True,
            secure=bool(cookie.secure),
            expires=int(expires),
            discard=False,
            comment=None,
            comment_url=None,
            rest={},
        )

        cj._cookies.setdefault(domain, {}).setdefault(
            cookie.path or "/", {}
        )[cookie.name] = c

    cj.save(ignore_discard=True, ignore_expires=True)

    log.info(f"[IG] Cookies export ho gayi → {output_file}")


# ─────────────────────────────────────────────────────────────
# SESSION STATUS
# ─────────────────────────────────────────────────────────────

def get_session_status():

    if not os.path.exists(SESSION_META_FILE):

        return {
            "logged_in": False,
            "message": "No active Instagram session"
        }

    try:

        with open(SESSION_META_FILE) as f:
            meta = json.load(f)

        return {
            "logged_in": True,
            "username": meta.get("username"),
            "saved_at": meta.get("saved_at"),
            "cookies_file": meta.get("cookies_file"),
            "message": (
                f"✅ Active session: "
                f"@{meta.get('username')}"
            )
        }

    except Exception as e:

        return {
            "logged_in": False,
            "message": f"Status fail: {e}"
        }


# ─────────────────────────────────────────────────────────────
# COOKIES FILE
# ─────────────────────────────────────────────────────────────

def get_cookies_file():

    if os.path.exists(COOKIES_FILE):
        return COOKIES_FILE

    # ✅ FIX: Absolute fallback path
    _alt = os.path.join(_IG_BOT_DIR, "instagram_cookies.txt")
    if os.path.exists(_alt):
        return _alt

    return ""


# ─────────────────────────────────────────────────────────────
# LOGOUT
# ─────────────────────────────────────────────────────────────

def logout():

    deleted = []

    try:

        if os.path.exists(SESSION_META_FILE):

            with open(SESSION_META_FILE) as f:
                meta = json.load(f)

            username = meta.get("username")

            sf = _session_file_path(username)

            if os.path.exists(sf):
                os.remove(sf)
                deleted.append(sf)

        for f in [COOKIES_FILE, SESSION_META_FILE]:

            if os.path.exists(f):
                os.remove(f)
                deleted.append(f)

        return {
            "success": True,
            "message": (
                "✅ Logout successful\n"
                f"Deleted: {deleted}"
            )
        }

    except Exception as e:

        return {
            "success": False,
            "error": str(e)
        }


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(message)s"
    )

    parser = argparse.ArgumentParser(
        description="Instagram Session Manager"
    )

    parser.add_argument(
        "--login",
        action="store_true"
    )

    parser.add_argument(
        "--refresh",
        action="store_true"
    )

    parser.add_argument(
        "--status",
        action="store_true"
    )

    parser.add_argument(
        "--logout",
        action="store_true"
    )

    parser.add_argument(
        "--user"
    )

    parser.add_argument(
        "--pass",
        dest="password"
    )

    args = parser.parse_args()

    if args.login:

        if not args.user or not args.password:

            print("❌ --user aur --pass required")

            exit(1)

        result = login_and_save(
            args.user,
            args.password
        )

        print(
            result.get("message")
            or result.get("error")
        )

    elif args.refresh:

        s = get_session_status()

        if not s.get("logged_in"):

            print("❌ No active session")

        else:

            ok = load_session(s["username"])

            print(
                "✅ Session refreshed"
                if ok else
                "❌ Refresh failed"
            )

    elif args.status:

        s = get_session_status()

        print(s["message"])

    elif args.logout:

        r = logout()

        print(
            r.get("message")
            or r.get("error")
        )

    else:

        parser.print_help()
