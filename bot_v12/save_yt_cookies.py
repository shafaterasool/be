"""
YouTube Cookies Exporter v2 — yt-dlp method
yt-dlp ka built-in browser extractor use karta hai
(Chrome encryption bhi handle karta hai)
"""

import os, sys, subprocess

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
OUTPUT_FILE = os.path.join(SCRIPT_DIR, "yt_cookies.txt")
TEST_URL    = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
BROWSERS    = ["chrome", "edge", "firefox", "brave", "opera", "chromium"]

def try_yt_dlp_browser(browser):
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--cookies-from-browser", browser,
        "--cookies", OUTPUT_FILE,
        "--skip-download",
        "--quiet",
        TEST_URL
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode == 0 and os.path.exists(OUTPUT_FILE) and os.path.getsize(OUTPUT_FILE) > 100:
        return True, ""
    err = (r.stderr or "").strip().splitlines()
    return False, (err[-1] if err else "unknown error")

def verify_cookies():
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--cookies", OUTPUT_FILE,
        "--skip-download", "--quiet",
        TEST_URL
    ]
    return subprocess.run(cmd, capture_output=True).returncode == 0

def main():
    print()
    print("╔══════════════════════════════════════════════════╗")
    print("║   YouTube Cookies Exporter v2 — bot_v12         ║")
    print("╚══════════════════════════════════════════════════╝")
    print()
    print("📌  Zaroori: Browser mein YouTube pe logged in hona chahiye")
    print(f"📁  Output:  {OUTPUT_FILE}")
    print()

    # yt-dlp check
    r = subprocess.run([sys.executable, "-m", "yt_dlp", "--version"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print("❌  yt-dlp nahi mila. Install karo:  pip install yt-dlp")
        sys.exit(1)
    print(f"✅  yt-dlp {r.stdout.strip()}")
    print()
    print("Browsers check kar raha hoon...")
    print()

    winner = None
    for b in BROWSERS:
        print(f"  🔍  {b.capitalize():10}", end=" ", flush=True)
        ok, err = try_yt_dlp_browser(b)
        if ok:
            print("✅  cookies mil gayi!")
            winner = b
            break
        e = err.lower()
        if   "not found" in e or "no such" in e or "failed to find" in e:
            print("— install nahi hai, skip.")
        elif "sign in" in e or "login" in e:
            print("— YouTube pe logged in nahi ho.")
        elif "admin" in e or "permission" in e or "access" in e:
            print("— Admin rights chahiye.")
        elif "decrypt" in e or "encrypt" in e:
            print("— Encryption block. Admin se chalao.")
        else:
            print(f"— {err[:70]}")

    print()

    if not winner:
        print("━" * 56)
        print("❌  Koi browser kaam nahi kiya.\n")
        print("  Sabse aasaan fix — CMD as Administrator:")
        print("  ┌─────────────────────────────────────────────────┐")
        print("  │  1. Start → cmd → Right click → Run as admin   │")
        print("  │  2. Yeh command chalao:                         │")
        print(f"  │                                                 │")
        print(f"  │  yt-dlp --cookies-from-browser edge  \\         │")
        print(f"  │    --cookies \"{OUTPUT_FILE}\" \\")
        print(f"  │    --skip-download                  \\          │")
        print(f"  │    https://www.youtube.com                      │")
        print("  └─────────────────────────────────────────────────┘")
        print()
        print("  YA browser extension se:")
        print("  1. Chrome mein install karo:")
        print("     'Get cookies.txt LOCALLY'")
        print("     (Chrome Web Store pe search karo)")
        print("  2. youtube.com kholo → Extension click karo")
        print("  3. Export → file yahan save karo:")
        print(f"     {OUTPUT_FILE}")
        print("━" * 56)
        sys.exit(1)

    print("🔍  Verify kar raha hoon...", end=" ", flush=True)
    if verify_cookies():
        print("✅  Perfect!\n")
        print("━" * 56)
        print("🎉  yt_cookies.txt bilkul ready hai!")
        print(f"    {OUTPUT_FILE}")
        print()
        print("    Bot restart karo — YouTube downloads ab")
        print("    bina bot-detection error ke chalenge. ✅")
        print("━" * 56)
    else:
        sz = os.path.getsize(OUTPUT_FILE) if os.path.exists(OUTPUT_FILE) else 0
        print(f"⚠️  File bani ({sz} bytes) lekin verify fail.")
        print(f"    Dobara try karo ya bot restart kar ke dekho.")
    print()

if __name__ == "__main__":
    main()