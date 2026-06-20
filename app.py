import os
import json
import uuid
import shutil
import socket
import re
import time
import random
from pathlib import Path

import requests as req_lib
from flask import Flask, render_template, request, jsonify, send_from_directory
import instaloader

app = Flask(__name__)
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)
SESSION_FILE = Path("session_config.json")
MEDIA_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".m4v"}


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "unknown"


def make_loader():
    L = instaloader.Instaloader(
        download_pictures=True,
        download_videos=True,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        post_metadata_txt_pattern=None,
        filename_pattern="{date_utc:%Y%m%d_%H%M%S}",
        sleep=True,
    )
    # Match the Firefox session: browser UA + web-app headers.
    # Using mobile UA with web cookies triggers Instagram's bot detection.
    L.context._session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) "
            "Gecko/20100101 Firefox/124.0"
        ),
        "X-IG-App-ID": "936619743392459",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.instagram.com/",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.5",
    })
    return L


def load_session_cookies(L):
    """
    Load Instagram session from env var (production) or session_config.json (local).
    Returns cookie rows compatible with _make_profile_session.
    """
    sessionid = os.environ.get("INSTAGRAM_SESSION_ID", "").strip()
    csrftoken  = os.environ.get("INSTAGRAM_CSRF_TOKEN",  "").strip()

    if not sessionid and SESSION_FILE.exists():
        try:
            cfg = json.loads(SESSION_FILE.read_text())
            sessionid = cfg.get("sessionid", "").strip()
            csrftoken  = cfg.get("csrftoken",  "").strip()
        except Exception:
            pass

    if not sessionid:
        raise Exception(
            "Instagram session not configured. "
            "Open the app, tap ⚙ Setup, and paste your sessionid."
        )

    L.context._session.cookies.set("sessionid", sessionid, domain=".instagram.com", path="/")
    if csrftoken:
        L.context._session.cookies.set("csrftoken", csrftoken, domain=".instagram.com", path="/")
        L.context._session.headers["X-CSRFToken"] = csrftoken

    rows = [("sessionid", sessionid, ".instagram.com", "/")]
    if csrftoken:
        rows.append(("csrftoken", csrftoken, ".instagram.com", "/"))
    return rows


def _make_profile_session(rows):
    """
    Build a clean requests.Session with Firefox browser headers and Instagram
    cookies set explicitly for www.instagram.com. Using instaloader's own
    session causes cookie-domain ambiguity; this session is unambiguous.
    """
    s = req_lib.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) "
            "Gecko/20100101 Firefox/124.0"
        ),
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "X-IG-App-ID": "936619743392459",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.instagram.com/",
        "Origin": "https://www.instagram.com",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "Connection": "keep-alive",
    })
    csrftoken = None
    has_session = False
    for name, value, host, path in rows:
        # Set under both the original Firefox host and explicitly www
        s.cookies.set(name, value, domain=host, path=path)
        s.cookies.set(name, value, domain="www.instagram.com", path=path)
        if name == "sessionid" and value:
            has_session = True
        if name == "csrftoken":
            csrftoken = value
    if not has_session:
        raise Exception(
            "No Instagram sessionid configured. "
            "Open ⚙ Session Setup and paste your sessionid."
        )
    if csrftoken:
        s.headers["X-CSRFToken"] = csrftoken
    return s


def _parse_codes_from_json(obj, seen=None):
    """Recursively walk any dict/list and collect all shortcode/code values."""
    if seen is None:
        seen = set()
    codes = []
    if isinstance(obj, dict):
        for key in ("shortcode", "code"):
            v = obj.get(key)
            if isinstance(v, str) and 8 <= len(v) <= 15 and re.fullmatch(r'[A-Za-z0-9_-]+', v):
                if v not in seen:
                    seen.add(v)
                    codes.append(v)
        for v in obj.values():
            codes += _parse_codes_from_json(v, seen)
    elif isinstance(obj, list):
        for item in obj:
            codes += _parse_codes_from_json(item, seen)
    return codes


def get_profile_post_codes(username, session, limit):
    target = int(limit) if limit else 12
    codes = []

    # ── Attempt 1: structured web_profile_info API ──────────────────────────
    try:
        r = session.get(
            "https://www.instagram.com/api/v1/users/web_profile_info/",
            params={"username": username},
            timeout=20,
        )
        if r.status_code == 200:
            data = r.json()
            # Handle both old (graphql.user) and new (data.user) shapes
            user = (
                data.get("data", {}).get("user")
                or data.get("graphql", {}).get("user")
                or {}
            )
            for tl_key in ("edge_owner_to_timeline_media", "timeline_media"):
                for edge in user.get(tl_key, {}).get("edges", []):
                    node = edge.get("node", {})
                    code = node.get("shortcode") or node.get("code")
                    if code and code not in codes:
                        codes.append(code)
    except Exception:
        pass

    # ── Attempt 2: profile page HTML — extract any JSON blobs ───────────────
    if not codes:
        try:
            r = session.get(
                f"https://www.instagram.com/{username}/",
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                    "Upgrade-Insecure-Requests": "1",
                },
                timeout=30,
            )
            if r.ok:
                html = r.text
                # Parse any <script type="application/json"> blobs
                for blob in re.findall(r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', html, re.DOTALL):
                    try:
                        codes += _parse_codes_from_json(json.loads(blob))
                    except Exception:
                        pass
                # Also catch flat "shortcode":"..." or "/p/CODE/" patterns
                if not codes:
                    for m in re.finditer(r'"shortcode"\s*:\s*"([A-Za-z0-9_-]{8,15})"', html):
                        if m.group(1) not in codes:
                            codes.append(m.group(1))
                if not codes:
                    for m in re.finditer(r'/p/([A-Za-z0-9_-]{8,15})/', html):
                        if m.group(1) not in codes:
                            codes.append(m.group(1))
        except Exception:
            pass

    if not codes:
        raise Exception(
            f"Could not find posts for @{username}. "
            "Possible causes: profile is private, your sessionid is expired, "
            "or Instagram changed their page format. "
            "Try refreshing your sessionid in ⚙ Session Setup."
        )

    return codes[:target]


def parse_url(url):
    """Returns ('post', shortcode), ('profile', username), or (None, None)."""
    url = url.strip().rstrip("/")
    m = re.search(r'instagram\.com/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)', url)
    if m:
        return "post", m.group(1)
    m = re.search(r'instagram\.com/([A-Za-z0-9_.]+)/?(?:\?.*)?$', url)
    if m and m.group(1) not in {
        "p", "reel", "reels", "tv", "stories", "explore", "accounts", "direct"
    }:
        return "profile", m.group(1)
    return None, None


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/fetch", methods=["POST"])
def fetch_media():
    data = request.get_json(silent=True) or {}
    url = data.get("url", "").strip()
    limit = data.get("limit", None)

    if not url or "instagram.com" not in url:
        return jsonify({"error": "Please provide a valid Instagram URL."}), 400

    url_type, identifier = parse_url(url)
    if not url_type:
        return jsonify({"error": "Could not parse that Instagram URL."}), 400

    session_id = str(uuid.uuid4())
    session_dir = DOWNLOAD_DIR / session_id
    session_dir.mkdir()

    try:
        L = make_loader()
        rows = load_session_cookies(L)

        if url_type == "post":
            post = instaloader.Post.from_shortcode(L.context, identifier)
            L.download_post(post, target=session_dir)
        else:
            profile_session = _make_profile_session(rows)
            codes = get_profile_post_codes(identifier, profile_session, limit)
            if not codes:
                raise Exception(f"No posts found for @{identifier}.")
            for code in codes:
                try:
                    post = instaloader.Post.from_shortcode(L.context, code)
                    L.download_post(post, target=session_dir)
                except Exception as e:
                    print(f"Skipping post {code}: {e}", flush=True)

        media_files = sorted([
            f for f in session_dir.rglob("*")
            if f.is_file() and f.suffix.lower() in MEDIA_EXTS
        ])

        if not media_files:
            shutil.rmtree(session_dir, ignore_errors=True)
            return jsonify({"error": "No media found. The post may be private or the URL is invalid."}), 404

        items = [
            {
                "url": f"/media/{session_id}/{f.relative_to(session_dir).as_posix()}",
                "type": "video" if f.suffix.lower() in {".mp4", ".mov", ".m4v"} else "image",
                "filename": f.name,
            }
            for f in media_files
        ]

        return jsonify({"session_id": session_id, "items": items, "count": len(items)})

    except instaloader.exceptions.LoginRequiredException:
        shutil.rmtree(session_dir, ignore_errors=True)
        return jsonify({"error": "Login required. Tap ⚙ Setup and re-enter your Instagram sessionid."}), 401
    except instaloader.exceptions.PrivateProfileNotFollowedException:
        shutil.rmtree(session_dir, ignore_errors=True)
        return jsonify({"error": "This account is private and you don't follow it."}), 403
    except instaloader.exceptions.BadResponseException as e:
        shutil.rmtree(session_dir, ignore_errors=True)
        return jsonify({"error": f"Instagram blocked the request. Wait a few minutes and try again. ({e})"}), 429
    except Exception as e:
        shutil.rmtree(session_dir, ignore_errors=True)
        return jsonify({"error": str(e)}), 500


@app.route("/media/<session_id>/<path:filename>")
def serve_media(session_id, filename):
    base = (DOWNLOAD_DIR / session_id).resolve()
    target = (base / filename).resolve()
    if not str(target).startswith(str(base)):
        return "Forbidden", 403
    return send_from_directory(base, filename)


@app.route("/api/session-status")
def session_status():
    sessionid = os.environ.get("INSTAGRAM_SESSION_ID", "").strip()
    if not sessionid and SESSION_FILE.exists():
        try:
            sessionid = json.loads(SESSION_FILE.read_text()).get("sessionid", "").strip()
        except Exception:
            pass
    return jsonify({"configured": bool(sessionid)})


@app.route("/api/save-session", methods=["POST"])
def save_session_config():
    data = request.get_json(silent=True) or {}
    sessionid = data.get("sessionid", "").strip()
    csrftoken  = data.get("csrftoken",  "").strip()
    if not sessionid:
        return jsonify({"error": "sessionid is required"}), 400
    SESSION_FILE.write_text(json.dumps({"sessionid": sessionid, "csrftoken": csrftoken}))
    return jsonify({"ok": True})


@app.route("/api/cleanup/<session_id>", methods=["DELETE"])
def cleanup(session_id):
    if not all(c in "0123456789abcdef-" for c in session_id):
        return jsonify({"error": "Invalid session"}), 400
    session_dir = DOWNLOAD_DIR / session_id
    if session_dir.exists():
        shutil.rmtree(session_dir)
    return jsonify({"ok": True})


@app.route("/api/debug-profile")
def debug_profile():
    """Return raw diagnostic info to help troubleshoot profile fetching."""
    username = request.args.get("username", "").strip()
    if not username:
        return jsonify({"error": "?username= required"}), 400

    result = {}
    try:
        L = make_loader()
        rows = load_session_cookies(L)
        s = _make_profile_session(rows)
    except Exception as e:
        return jsonify({"error": f"Session setup failed: {e}"}), 500

    # Test web_profile_info
    try:
        r = s.get(
            "https://www.instagram.com/api/v1/users/web_profile_info/",
            params={"username": username},
            timeout=20,
        )
        result["api_status"] = r.status_code
        result["api_preview"] = r.text[:500]
    except Exception as e:
        result["api_error"] = str(e)

    # Test profile page
    try:
        r2 = s.get(
            f"https://www.instagram.com/{username}/",
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
            },
            timeout=30,
        )
        result["page_status"] = r2.status_code
        result["page_length"] = len(r2.text)
        # Count how many shortcodes we'd find
        sc_count = len(re.findall(r'"shortcode"\s*:\s*"[A-Za-z0-9_-]{8,15}"', r2.text))
        code_count = len(re.findall(r'/p/([A-Za-z0-9_-]{8,15})/', r2.text))
        result["shortcodes_found"] = sc_count
        result["post_urls_found"] = code_count
    except Exception as e:
        result["page_error"] = str(e)

    return jsonify(result)


if __name__ == "__main__":
    local_ip = get_local_ip()
    print("\n" + "=" * 50)
    print("  InstaGet is running!")
    print("=" * 50)
    print(f"  Local : http://localhost:5000")
    print(f"  Network: http://{local_ip}:5000")
    print("=" * 50 + "\n")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
