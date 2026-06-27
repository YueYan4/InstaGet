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
        "Accept-Encoding": "gzip, deflate",
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


def _make_browser_session(rows):
    """
    Minimal browser session for page fetches and CDN downloads.
    No Instagram API headers — those cause redirect loops on page navigation.
    """
    s = req_lib.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) "
            "Gecko/20100101 Firefox/124.0"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
    })
    for name, value, host, path in rows:
        s.cookies.set(name, value, domain=host, path=path)
        s.cookies.set(name, value, domain="www.instagram.com", path=path)
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


def _find_next_cursor(obj):
    """Return end_cursor where has_next_page is True, or None."""
    if isinstance(obj, dict):
        pi = obj.get("page_info")
        if isinstance(pi, dict) and pi.get("has_next_page") and pi.get("end_cursor"):
            return pi["end_cursor"]
        if obj.get("has_next_page") and obj.get("end_cursor"):
            return obj["end_cursor"]
        for v in obj.values():
            r = _find_next_cursor(v)
            if r:
                return r
    elif isinstance(obj, list):
        for item in obj:
            r = _find_next_cursor(item)
            if r:
                return r
    return None


def _codes_and_cursor_from_html(html, existing=None):
    """Parse all JSON blobs from an Instagram HTML page; return (codes, cursor)."""
    seen = set(existing or [])
    codes = []
    cursor = None
    for blob in re.findall(
        r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', html, re.DOTALL
    ):
        try:
            parsed = json.loads(blob)
            codes += _parse_codes_from_json(parsed, seen)
            seen.update(codes)
            if not cursor:
                cursor = _find_next_cursor(parsed)
        except Exception:
            pass
    return codes, cursor


def get_profile_post_codes(username, session, limit):
    target = int(limit) if limit else 12
    codes = []
    items_by_code = {}
    diag = {}
    html = ""
    lsd = None
    user_id = None

    # ── Step 0: fetch profile page (lsd cookie, user_id, HTML fallback) ─────
    try:
        pr = session.get(
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
        diag["page_status"] = pr.status_code
        if pr.ok:
            html = pr.text
            diag["page_length"] = len(html)

            # Meta/Instagram sets lsd as a cookie on page load
            lsd = (session.cookies.get("lsd", domain="www.instagram.com")
                   or session.cookies.get("lsd"))

            # Fall back to scanning the HTML for the token
            if not lsd:
                for pat in [
                    r'"LSD",\[\],\{"token":"([^"]+)"',
                    r'"LSD",\[\],"token","([^"]+)"',
                    r'"LSD",\[\],"([A-Za-z0-9_-]{8,25})"',
                    r'"lsd"\s*:\s*"([A-Za-z0-9_-]{8,25})"',
                    r'name="lsd"\s+value="([^"]+)"',
                ]:
                    m = re.search(pat, html)
                    if m:
                        lsd = m.group(1)
                        break

            # Extract numeric user_id
            for pat in [
                r'"owner_id"\s*:\s*"(\d{6,15})"',
                r'"user_id"\s*:\s*"(\d{6,15})"',
                r'"ds_user_id"\s*:\s*"(\d{6,15})"',
                r'"pk"\s*:\s*"(\d{6,15})"',
            ]:
                m = re.search(pat, html)
                if m:
                    user_id = m.group(1)
                    break

            diag["lsd_found"] = bool(lsd)
            diag["user_id_found"] = bool(user_id)
            if user_id:
                diag["user_id"] = user_id
    except Exception as e:
        diag["page_error"] = str(e)

    # ── Attempt A: ?__a=1 first page ────────────────────────────────────────
    profile_total = None
    a1_cursor = None
    a1_user_id = None
    if not codes:
        try:
            r = session.get(
                f"https://www.instagram.com/{username}/",
                params={"__a": "1"},
                headers={"Accept": "application/json, text/javascript, */*"},
                timeout=20,
            )
            diag["a1_status"] = r.status_code
            diag["a1_preview"] = r.text[:300]
            if 200 <= r.status_code < 300:
                try:
                    data = r.json()
                    user_node = (
                        data.get("graphql", {}).get("user")
                        or data.get("data", {}).get("user")
                        or {}
                    )
                    a1_user_id = user_node.get("id")
                    tl = user_node.get("edge_owner_to_timeline_media", {})
                    profile_total = tl.get("count")
                    for edge in tl.get("edges", []):
                        code = (edge.get("node", {}).get("shortcode")
                                or edge.get("node", {}).get("code"))
                        if code and code not in codes:
                            codes.append(code)
                    page_info = tl.get("page_info", {})
                    a1_cursor = page_info.get("end_cursor") if page_info.get("has_next_page") else None
                    diag["a1_codes"] = len(codes)
                    diag["a1_has_next"] = page_info.get("has_next_page")
                    diag["a1_cursor"] = bool(a1_cursor)
                    diag["a1_user_id"] = a1_user_id
                    print(f"[profile] page=1 codes={len(codes)} total={profile_total} "
                          f"has_next={page_info.get('has_next_page')} cursor={'yes' if a1_cursor else 'none'}",
                          flush=True)
                except Exception as e:
                    diag["a1_json_error"] = str(e)
        except Exception as e:
            diag["a1_error"] = str(e)

    # ── Attempt A2: paginate via feed/user/{id}/ with cursor from A ─────────
    if codes and len(codes) < target and (a1_cursor or a1_user_id):
        uid = a1_user_id or user_id
        cursor = a1_cursor
        page = 2
        while uid and len(codes) < target:
            time.sleep(random.uniform(4, 7))  # generous delay to avoid 429
            params = {"count": min(12, target - len(codes))}
            if cursor:
                params["max_id"] = cursor
            # Try feed/user endpoint first, fall back to ?__a=1 with max_id
            for url, extra_params in [
                (f"https://www.instagram.com/api/v1/feed/user/{uid}/", params),
                (f"https://www.instagram.com/{username}/", {"__a": "1", **params}),
            ]:
                try:
                    r = session.get(
                        url, params=extra_params,
                        headers={"Accept": "application/json, text/javascript, */*"},
                        timeout=20,
                    )
                    diag[f"page{page}_status"] = r.status_code
                    if r.status_code == 429:
                        time.sleep(10)
                        r = session.get(url, params=extra_params,
                                        headers={"Accept": "application/json, text/javascript, */*"},
                                        timeout=20)
                        diag[f"page{page}_retry"] = r.status_code
                    if not (200 <= r.status_code < 300):
                        continue
                    data = r.json()
                    # feed/user returns items[], ?__a=1 returns graphql.user
                    new_codes = []
                    items = data.get("items")
                    if items is not None:
                        for item in items:
                            c = item.get("code") or item.get("shortcode")
                            if c and c not in codes:
                                new_codes.append(c)
                                items_by_code[c] = item
                        pg = {}
                        cursor = data.get("next_max_id") if data.get("more_available") else None
                    else:
                        unode = (data.get("graphql", {}).get("user")
                                 or data.get("data", {}).get("user") or {})
                        tl2 = unode.get("edge_owner_to_timeline_media", {})
                        for edge in tl2.get("edges", []):
                            c = edge.get("node", {}).get("shortcode") or edge.get("node", {}).get("code")
                            if c and c not in codes:
                                new_codes.append(c)
                        pi2 = tl2.get("page_info", {})
                        cursor = pi2.get("end_cursor") if pi2.get("has_next_page") else None
                    codes.extend(new_codes)
                    diag[f"page{page}_codes"] = len(new_codes)
                    print(f"[profile] page={page} new={len(new_codes)} total={len(codes)} "
                          f"cursor={'yes' if cursor else 'none'}", flush=True)
                    break  # one of the two URLs worked
                except Exception as e:
                    diag[f"page{page}_err"] = str(e)
            if not cursor:
                break
            page += 1

    # ── Attempt B: feed/user/{id}/ with pagination ──────────────────────────
    if not codes and user_id:
        feed_cursor = None
        feed_page = 1
        while len(codes) < target:
            try:
                params = {"count": min(12, target - len(codes))}
                if feed_cursor:
                    params["max_id"] = feed_cursor
                r = session.get(
                    f"https://www.instagram.com/api/v1/feed/user/{user_id}/",
                    params=params,
                    timeout=20,
                )
                if feed_page == 1:
                    diag["feed_status"] = r.status_code
                if r.status_code == 429:
                    diag[f"feed_p{feed_page}_429"] = True
                    time.sleep(10)
                    continue
                if r.status_code != 200:
                    diag["feed_preview"] = r.text[:200]
                    break
                data = r.json()
                new_items = 0
                for item in data.get("items", []):
                    code = item.get("code") or item.get("shortcode")
                    if code and code not in codes:
                        codes.append(code)
                        items_by_code[code] = item
                        new_items += 1
                diag[f"feed_p{feed_page}"] = new_items
                print(f"[profile] feed page={feed_page} new={new_items} total={len(codes)}", flush=True)
                if not data.get("more_available") or not data.get("next_max_id"):
                    break
                feed_cursor = data["next_max_id"]
                feed_page += 1
                if len(codes) < target:
                    time.sleep(random.uniform(3, 5))
            except Exception as e:
                diag[f"feed_p{feed_page}_err"] = str(e)
                break
        diag["feed_codes"] = len(codes)

    # ── Attempt C: lsd + /api/graphql POST ──────────────────────────────────
    if not codes and lsd:
        for doc_id in ("7950326721671168", "8508961505838141", "17880160963012870"):
            if codes:
                break
            try:
                r = session.post(
                    "https://www.instagram.com/api/graphql",
                    data={
                        "lsd": lsd,
                        "variables": json.dumps({"data": {"count": min(target, 12)}, "username": username}),
                        "doc_id": doc_id,
                        "server_timestamps": "true",
                        "fb_api_caller_class": "RelayModern",
                    },
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "X-FB-LSD": lsd,
                        "Sec-Fetch-Dest": "empty",
                        "Sec-Fetch-Mode": "cors",
                        "Sec-Fetch-Site": "same-origin",
                    },
                    timeout=20,
                )
                diag[f"gql_{doc_id}"] = r.status_code
                if r.status_code == 200:
                    found = _parse_codes_from_json(r.json())
                    codes.extend(c for c in found if c not in codes)
                else:
                    diag[f"gql_{doc_id}_preview"] = r.text[:150]
            except Exception as e:
                diag[f"gql_{doc_id}_err"] = str(e)

    # ── Attempt D: web_profile_info ──────────────────────────────────────────
    if not codes:
        try:
            r = session.get(
                "https://www.instagram.com/api/v1/users/web_profile_info/",
                params={"username": username},
                timeout=20,
            )
            diag["api_status"] = r.status_code
            if r.status_code == 200:
                data = r.json()
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
            else:
                diag["api_preview"] = r.text[:150]
        except Exception as e:
            diag["api_error"] = str(e)

    # ── Attempt E: JSON blobs in page HTML + cursor-based HTML pagination ────
    if not codes and html:
        new_codes, html_cursor = _codes_and_cursor_from_html(html)
        codes.extend(c for c in new_codes if c not in codes)
        diag["html_codes"] = len(codes)
        diag["html_cursor"] = bool(html_cursor)
        print(f"[profile] html page=1 codes={len(codes)} cursor={'yes' if html_cursor else 'no'}",
              flush=True)

        # Paginate by requesting ?max_id=CURSOR — Instagram SSR renders next posts
        page = 2
        cursor = html_cursor
        while codes and len(codes) < target and cursor:
            time.sleep(random.uniform(3, 5))
            try:
                r = session.get(
                    f"https://www.instagram.com/{username}/",
                    params={"max_id": cursor},
                    headers={
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Sec-Fetch-Dest": "document",
                        "Sec-Fetch-Mode": "navigate",
                        "Sec-Fetch-Site": "none",
                    },
                    timeout=30,
                )
                diag[f"html_p{page}_status"] = r.status_code
                if not r.ok:
                    break
                new_codes, cursor = _codes_and_cursor_from_html(r.text, existing=codes)
                codes.extend(c for c in new_codes if c not in codes)
                diag[f"html_p{page}_new"] = len(new_codes)
                print(f"[profile] html page={page} new={len(new_codes)} total={len(codes)} "
                      f"cursor={'yes' if cursor else 'no'}", flush=True)
                if not new_codes:
                    break
            except Exception as e:
                diag[f"html_p{page}_err"] = str(e)
                break
            page += 1

    if not codes:
        raise Exception(
            f"Could not load posts for @{username}. Debug: {json.dumps(diag)}"
        )

    return codes[:target], items_by_code, profile_total, diag


_SC_ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_'

def shortcode_to_mediaid(shortcode):
    n = 0
    for c in shortcode:
        n = n * 64 + _SC_ALPHABET.index(c)
    return n


def _save_url(session, url, path):
    print(f"[_save_url] {url[:120]}", flush=True)
    try:
        r = session.get(url, stream=True, timeout=60, headers={"Accept": "image/webp,*/*"})
        r.raise_for_status()
    except Exception as e:
        raise Exception(f"CDN fetch failed ({url[:80]}): {e}")
    with open(path, "wb") as f:
        for chunk in r.iter_content(65536):
            f.write(chunk)


def _save_media_node(node, session, dest, prefix, idx):
    mt = node.get("media_type", 1)
    if mt == 2:  # video
        versions = node.get("video_versions", [])
        url = versions[0]["url"] if versions else None
        ext = "mp4"
    else:  # image
        candidates = node.get("image_versions2", {}).get("candidates", [])
        url = candidates[0]["url"] if candidates else None
        ext = "jpg"
    if url:
        _save_url(session, url, dest / f"{prefix}_{idx:02d}.{ext}")


def _extract_post_media(obj, seen=None):
    """Walk JSON blobs from a post page; collect (ext, url) for each media item."""
    if seen is None:
        seen = set()
    result = []
    if isinstance(obj, dict):
        vurl = obj.get("video_url")
        durl = obj.get("display_url")
        if isinstance(vurl, str) and vurl.startswith("https://") and vurl not in seen:
            seen.add(vurl)
            result.append(("mp4", vurl))
            if isinstance(durl, str):
                seen.add(durl)  # suppress thumbnail from this same node
        elif isinstance(durl, str) and durl.startswith("https://") and durl not in seen:
            seen.add(durl)
            result.append(("jpg", durl))
        for v in obj.values():
            if isinstance(v, (dict, list)):
                result.extend(_extract_post_media(v, seen))
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                result.extend(_extract_post_media(item, seen))
    return result


_PLAIN_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) "
        "Gecko/20100101 Firefox/124.0"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}


def _fetch_og_meta(shortcode):
    """
    Fetch the post page as Facebook’s link-preview crawler.
    Instagram serves OG metadata to this UA, giving us og:title (which contains
    the username) and og:image (thumbnail) without cookies or redirects.
    Returns (username_or_None, og_image_url_or_None).
    """
    r = req_lib.get(
        f"https://www.instagram.com/p/{shortcode}/",
        headers={
            "User-Agent": (
                "facebookexternalhit/1.1 "
                "(+http://www.facebook.com/externalhit_uatext.php)"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate",
        },
        timeout=30,
    )
    if not r.ok:
        print(f"[og_meta] HTTP {r.status_code}", flush=True)
        return None, None

    html = r.text
    print(f"[og_meta] status={r.status_code} len={len(html)}", flush=True)

    username = None
    # og:title is "@username on Instagram: …" or "username on Instagram: …"
    for pat in [
        r’<meta\s+property="og:title"\s+content="([^"]*)"’,
        r’<meta\s+content="([^"]*)"\s+property="og:title"’,
    ]:
        m = re.search(pat, html)
        if m:
            title = (m.group(1)
                     .replace(‘&amp;’, ‘&’)
                     .replace(‘&#039;’, "’")
                     .replace(‘&quot;’, ‘"’))
            um = re.match(r’@?([A-Za-z0-9_.]+)\s+on\s+Instagram’, title, re.IGNORECASE)
            if um:
                username = um.group(1)
                print(f"[og_meta] username={username}", flush=True)
            break

    og_image = None
    for pat in [
        r’<meta\s+property="og:image"\s+content="([^"]*)"’,
        r’<meta\s+content="([^"]*)"\s+property="og:image"’,
    ]:
        m = re.search(pat, html)
        if m:
            og_image = m.group(1).replace(‘&amp;’, ‘&’)
            break

    return username, og_image


def _get_single_post_item(shortcode, session, hint_username=None):
    """
    Resolve a single post shortcode to a feed item dict.
    Strategy:
      1. hint_username (from URL format) OR facebookexternalhit OG fetch → username
      2. Profile page HTML → user_id
      3. feed/user/{id}/ search → full-resolution item
    Fallback: if username found but post not in feed, return synthetic item
              with the OG thumbnail so the user gets something.
    """
    username = hint_username
    og_image_url = None

    if not username:
        try:
            username, og_image_url = _fetch_og_meta(shortcode)
        except Exception as e:
            print(f"[og_meta] exception: {e}", flush=True)

    # Full-resolution path via feed/user
    if username:
        pr = session.get(
            f"https://www.instagram.com/{username}/",
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
            },
            timeout=30,
        )
        user_id = None
        for pat in [
            r’"owner_id"\s*:\s*"(\d{6,15})"’,
            r’"user_id"\s*:\s*"(\d{6,15})"’,
            r’"ds_user_id"\s*:\s*"(\d{6,15})"’,
            r’"pk"\s*:\s*"(\d{6,15})"’,
        ]:
            m = re.search(pat, pr.text)
            if m:
                user_id = m.group(1)
                break

        if user_id:
            cursor = None
            for _ in range(10):
                params = {"count": 12}
                if cursor:
                    params["max_id"] = cursor
                r = session.get(
                    f"https://www.instagram.com/api/v1/feed/user/{user_id}/",
                    params=params,
                    timeout=20,
                )
                r.raise_for_status()
                data = r.json()
                for item in data.get("items", []):
                    if item.get("code") == shortcode or item.get("shortcode") == shortcode:
                        return item
                if not data.get("more_available") or not data.get("next_max_id"):
                    break
                cursor = data["next_max_id"]
                time.sleep(random.uniform(2, 4))

    # Fallback: OG thumbnail (lower resolution but functional)
    if og_image_url:
        print(f"[og_meta] using thumbnail fallback", flush=True)
        return {
            "media_type": 1,
            "image_versions2": {
                "candidates": [{"url": og_image_url}]
            },
            "code": shortcode,
        }

    raise Exception(
        "Could not download this post from the server. "
        "Try the profile URL (instagram.com/username/) instead."
    )


def _save_url_plain(url, path):
    """Download a CDN URL with no session cookies."""
    r = req_lib.get(url, stream=True, timeout=60,
                    headers={**_PLAIN_HEADERS, "Accept": "image/webp,*/*"})
    r.raise_for_status()
    with open(path, "wb") as f:
        for chunk in r.iter_content(65536):
            f.write(chunk)


def _scrape_urls_from_html(html):
    seen = set()
    urls = []

    # 1. application/json script blobs
    for blob in re.findall(
        r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', html, re.DOTALL
    ):
        try:
            urls.extend(_extract_post_media(json.loads(blob), seen))
        except Exception:
            pass

    # 2. window.__additionalDataLoaded('/p/...', {...})
    for data_str in re.findall(
        r'window\.__additionalDataLoaded\([^,]+,\s*(\{.+?\})\s*\)', html, re.DOTALL
    ):
        try:
            urls.extend(_extract_post_media(json.loads(data_str), seen))
        except Exception:
            pass

    # 3. <img src> pointing to Instagram/Facebook CDN
    for u in re.findall(r'<img[^>]+\bsrc="(https://[^"]+)"', html):
        if re.search(r'(cdninstagram\.com|fbcdn\.net|scontent)', u) and u not in seen:
            seen.add(u)
            urls.append(('jpg', u))

    # 4. <video src>
    for u in re.findall(r'<video[^>]+\bsrc="(https://[^"]+)"', html):
        if u not in seen:
            seen.add(u)
            urls.append(('mp4', u))

    return urls


def download_post_via_html(shortcode, session, dest_dir):
    """Download single post without touching any rate-limited API endpoint."""
    errors = []

    # Strategy 1: embed page — no auth needed, designed for external embedding
    try:
        r = req_lib.get(
            f"https://www.instagram.com/p/{shortcode}/embed/captioned/",
            headers={**_PLAIN_HEADERS,
                     "Accept": "text/html,application/xhtml+xml,*/*"},
            timeout=30,
        )
        if r.ok:
            urls = _scrape_urls_from_html(r.text)
            if urls:
                for i, (ext, url) in enumerate(urls):
                    _save_url_plain(url, dest_dir / f"{shortcode}_{i:02d}.{ext}")
                return
            errors.append("embed: no media URLs in JSON blobs")
        else:
            errors.append(f"embed: HTTP {r.status_code}")
    except Exception as e:
        errors.append(f"embed: {e}")

    # Strategy 2: ?__a=1 JSON endpoint without session (cookies cause redirect loops)
    try:
        r = req_lib.get(
            f"https://www.instagram.com/p/{shortcode}/",
            params={"__a": "1"},
            headers={**_PLAIN_HEADERS, "Accept": "application/json, */*"},
            timeout=20,
        )
        if r.ok and r.text.strip():
            data = r.json()
            node = (data.get("graphql", {}).get("shortcode_media")
                    or (data.get("items") or [None])[0])
            if node:
                urls = _extract_post_media(node)
                if urls:
                    for i, (ext, url) in enumerate(urls):
                        _save_url_plain(url, dest_dir / f"{shortcode}_{i:02d}.{ext}")
                    return
        errors.append(f"?__a=1: HTTP {r.status_code} body={r.text[:100]}")
    except Exception as e:
        errors.append(f"?__a=1: {e}")

    raise Exception(f"single post download failed — {'; '.join(errors)}")


def _load_session_rows():
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
            "Instagram session not configured. Open ⚙ Setup and paste your sessionid."
        )
    rows = [("sessionid", sessionid, ".instagram.com", "/")]
    if csrftoken:
        rows.append(("csrftoken", csrftoken, ".instagram.com", "/"))
    return rows


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
    body_hint_username = (data.get("hint_username") or "").strip().lstrip("@") or None

    if not url or "instagram.com" not in url:
        return jsonify({"error": "Please provide a valid Instagram URL."}), 400

    url_type, identifier = parse_url(url)
    if not url_type:
        return jsonify({"error": "Could not parse that Instagram URL."}), 400

    # Resolve hint_username: prefer explicit body param, then URL-embedded username
    url_hint_username = body_hint_username
    if not url_hint_username and url_type == "post":
        um = re.search(r'instagram\.com/([A-Za-z0-9_.]+)/(?:p|reel|reels|tv)/', url)
        if um and um.group(1) not in {
            "p", "reel", "reels", "tv", "stories", "explore", "accounts", "direct"
        }:
            url_hint_username = um.group(1)

    session_id = str(uuid.uuid4())
    session_dir = DOWNLOAD_DIR / session_id
    session_dir.mkdir()

    try:
        rows = _load_session_rows()
        session = _make_profile_session(rows)   # for Instagram API calls
        browser = _make_browser_session(rows)   # for page fetches + CDN downloads

        profile_total = None
        profile_diag = {}
        items_by_code = {}
        if url_type == "post":
            codes = [identifier]
        else:
            codes, items_by_code, profile_total, profile_diag = get_profile_post_codes(
                identifier, session, limit
            )
            if not codes:
                raise Exception(f"No posts found for @{identifier}.")

        download_errors = []
        for code in codes:
            try:
                print(f"[download] {code}", flush=True)
                item = items_by_code.get(code)
                if item is None:
                    # Single post URL: resolve via hint/oembed → profile page → feed/user
                    item = _get_single_post_item(code, session, url_hint_username)
                if item.get("media_type") == 8:
                    for i, child in enumerate(item.get("carousel_media", [])):
                        _save_media_node(child, browser, session_dir, code, i)
                else:
                    _save_media_node(item, browser, session_dir, code, 0)
                time.sleep(random.uniform(1, 3))
            except Exception as e:
                msg = f"{code}: {e}"
                print(f"Skipping {msg}", flush=True)
                download_errors.append(msg)

        media_files = sorted([
            f for f in session_dir.rglob("*")
            if f.is_file() and f.suffix.lower() in MEDIA_EXTS
        ])

        if not media_files:
            shutil.rmtree(session_dir, ignore_errors=True)
            detail = "; ".join(download_errors[:2]) if download_errors else "no details"
            return jsonify({"error": f"No media saved. {detail}"}), 404

        items = [
            {
                "url": f"/media/{session_id}/{f.relative_to(session_dir).as_posix()}",
                "type": "video" if f.suffix.lower() in {".mp4", ".mov", ".m4v"} else "image",
                "filename": f.name,
            }
            for f in media_files
        ]

        resp = {"session_id": session_id, "items": items, "count": len(items)}
        if profile_total is not None:
            resp["profile_total"] = profile_total
        if url_type == "profile":
            resp["profile_diag"] = {
                k: profile_diag[k]
                for k in ("a1_status", "a1_json_error",
                          "user_id_found", "feed_status", "feed_codes",
                          "feed_p1", "feed_p2", "feed_p3",
                          "html_codes", "html_cursor",
                          "api_status")
                if k in profile_diag
            }
        return jsonify(resp)

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
