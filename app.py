#!/usr/bin/env python3
"""GolfWatch - single-file Kijiji golf monitor + live web page.
Waterloo ON, 40km radius. Background poller feeds a Flask page."""

import json
import math
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from xml.etree import ElementTree

import requests
from flask import Flask, render_template_string

KEYWORD = "golf"
CENTER_LAT = 43.4643
CENTER_LNG = -80.5204
RADIUS_KM = 40
POLL_SECONDS = 120
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golfwatch.db")
BLOCKLIST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blocklist.json")

# Kijiji killed RSS (2024) and ignores keyword params on some endpoints,
# so we try several known search-URL formats each poll and stick with
# whichever one actually returns keyword-matching listings.
SEARCH_URLS = [
    f"https://www.kijiji.ca/b-buy-sell/kitchener-waterloo/{KEYWORD}/k0c10l1700212"
    f"?sort=dateDesc&address=Waterloo%2C+ON&radius={RADIUS_KM}.0",
    f"https://www.kijiji.ca/b-kitchener-waterloo/{KEYWORD}/k0l1700212?sort=dateDesc",
    "https://www.kijiji.ca/b-search.html"
    f"?searchKeyword={KEYWORD}&q={KEYWORD}&address=Waterloo%2C+ON"
    f"&ll={CENTER_LAT}%2C{CENTER_LNG}&radius={RADIUS_KM}&sort=dateDesc",
]
_good_url_idx = [None]
def _ensure_curl_cffi():
    """Install curl_cffi at runtime (keeps the Railway start command simple)."""
    try:
        import curl_cffi  # noqa: F401
        return True
    except ImportError:
        pass
    import subprocess, sys
    for pkg in ("curl-cffi", "curl_cffi"):
        try:
            print(f"installing {pkg}...", flush=True)
            r = subprocess.run([sys.executable, "-m", "pip", "install",
                                "--no-cache-dir", pkg],
                               capture_output=True, text=True, timeout=300)
            if r.returncode == 0:
                print("curl_cffi installed", flush=True)
                return True
            print(f"pip {pkg} failed: {r.stderr[-500:]}", flush=True)
        except Exception as e:
            print(f"pip {pkg} error: {e}", flush=True)
    return False


try:
    if not _ensure_curl_cffi():
        raise ImportError("curl_cffi unavailable")
    from curl_cffi import requests as cf_requests
    _session = cf_requests.Session()
    def _get(url):
        return _session.get(url, impersonate="chrome", timeout=25, headers={
            "Accept-Language": "en-CA,en;q=0.9",
            "Referer": "https://www.kijiji.ca/",
        })
except ImportError:
    _session = requests.Session()
    def _get(url):
        return _session.get(url, timeout=25, headers=HEADERS)


def _walk(node, found):
    """Recursively collect listing-like dicts from __NEXT_DATA__ JSON."""
    if isinstance(node, dict):
        keys = set(node.keys())
        if ("title" in keys and ("id" in keys or "adId" in keys)
                and ("price" in keys or "url" in keys or "seoUrl" in keys)):
            found.append(node)
        for v in node.values():
            _walk(v, found)
    elif isinstance(node, list):
        for v in node:
            _walk(v, found)


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def parse_feed(html_text):
    """Yield listing dicts from the embedded Next.js JSON on the search page."""
    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html_text, re.S)
    if not m:
        print("parse: __NEXT_DATA__ not found (page layout changed?)", flush=True)
        return
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        print(f"parse: bad JSON in __NEXT_DATA__: {e}", flush=True)
        return

    found = []
    _walk(data.get("props", data), found)
    seen_ids = set()
    for node in found:
        lid = str(node.get("id") or node.get("adId") or "")
        if not lid or lid in seen_ids:
            continue
        seen_ids.add(lid)

        title = str(node.get("title") or "").strip()
        if not title or KEYWORD.lower() not in title.lower():
            continue  # hard keyword filter: page may include unrelated modules

        url = node.get("url") or node.get("seoUrl") or ""
        if url and url.startswith("/"):
            url = "https://www.kijiji.ca" + url

        price = node.get("price")
        if isinstance(price, dict):
            amt = price.get("amount")
            price = f"${amt/100:,.2f}" if isinstance(amt, (int, float)) else \
                    str(price.get("text") or "n/a")
        elif isinstance(price, (int, float)):
            price = f"${price/100:,.2f}" if price > 10000 else f"${price:,.2f}"
        else:
            price = str(price) if price else "n/a"

        lat = lng = None
        loc = node.get("location")
        if isinstance(loc, dict):
            coords = loc.get("coordinates") or loc
            lat = _num(coords.get("latitude") if isinstance(coords, dict) else None)
            lng = _num(coords.get("longitude") if isinstance(coords, dict) else None)

        yield {
            "id": lid, "title": title, "url": url, "price": price,
            "lat": lat, "lng": lng,
            "published": str(node.get("activationDate") or node.get("sortingDate") or ""),
        }


def haversine_km(lat1, lng1, lat2, lng2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS seen_listings ("
        " listing_id TEXT PRIMARY KEY,"
        " title TEXT, price TEXT, url TEXT,"
        " distance_km REAL, seen_at TEXT)"
    )
    conn.execute(
        "DELETE FROM seen_listings WHERE title IS NOT NULL "
        "AND lower(title) NOT LIKE '%' || ? || '%'", (KEYWORD.lower(),))
    conn.commit()
    return conn


def load_blocklist():
    try:
        with open(BLOCKLIST_PATH) as f:
            return [s.lower() for s in json.load(f)]
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def notify(listing, dist_txt):
    line = f"⛳ NEW: {listing['title']} — {listing['price']} — {dist_txt} — {listing['url']}"
    print(line, flush=True)


# ----------------- main cycle -----------------
def run_once(conn, blocklist, first_run=False):
    order = list(range(len(SEARCH_URLS)))
    if _good_url_idx[0] is not None:
        order.remove(_good_url_idx[0])
        order.insert(0, _good_url_idx[0])
    listings_batch = None
    for idx in order:
        try:
            resp = _get(SEARCH_URLS[idx])
            resp.raise_for_status()
        except Exception as e:
            print(f"[{datetime.now():%H:%M:%S}] url{idx} fetch failed: {e}", flush=True)
            continue
        batch = list(parse_feed(resp.text))
        print(f"[{datetime.now():%H:%M:%S}] url{idx} -> {len(batch)} '{KEYWORD}' listings", flush=True)
        if batch:
            _good_url_idx[0] = idx
            listings_batch = batch
            break
    if listings_batch is None:
        print(f"[{datetime.now():%H:%M:%S}] no URL format returned matches this cycle", flush=True)
        return

    new_count = 0
    for listing in listings_batch:
        cur = conn.execute(
            "SELECT 1 FROM seen_listings WHERE listing_id=?", (listing["id"],)
        )
        if cur.fetchone():
            continue

        # distance filter (Kijiji radius is decent but trust-but-verify)
        dist = None
        if listing["lat"] is not None:
            dist = haversine_km(CENTER_LAT, CENTER_LNG, listing["lat"], listing["lng"])
        dist_txt = f"{dist:.1f} km" if dist is not None else "distance unknown"
        in_range = dist is None or dist <= RADIUS_KM

        # blocklist check against title text (seller name needs detail-page
        # fetch — Phase 3; title match catches obvious repeat spammers now)
        blocked = any(b in listing["title"].lower() for b in blocklist)

        conn.execute(
            "INSERT OR IGNORE INTO seen_listings VALUES (?,?,?,?,?,?)",
            (listing["id"], listing["title"], listing["price"], listing["url"],
             dist, datetime.now(timezone.utc).isoformat()),
        )
        new_count += 1

        if first_run:
            continue  # seed silently — don't spam alerts for pre-existing ads
        if in_range and not blocked:
            notify(listing, dist_txt)

    conn.commit()
    stamp = f"[{datetime.now():%H:%M:%S}]"
    if first_run:
        print(f"{stamp} seeded {new_count} existing listings (silent). "
              f"New ones will pop up from now on.", flush=True)
    else:
        print(f"{stamp} checked feed — {new_count} new", flush=True)




app = Flask(__name__)

PAGE = """
<!doctype html><html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>GolfWatch — Waterloo</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: -apple-system, system-ui, sans-serif; background:#0f1a12;
         color:#e8f0e8; max-width:720px; margin:0 auto; padding:16px; }
  h1 { font-size:1.3rem; } h1 span { color:#4ade80; }
  .sub { color:#9ab39f; font-size:.85rem; margin-bottom:1rem; }
  .card { background:#16241a; border:1px solid #234d2c; border-radius:10px;
          padding:12px 14px; margin-bottom:10px; }
  .card.new { border-color:#4ade80; }
  .title { font-weight:600; text-decoration:none; color:#e8f0e8; }
  .meta { color:#9ab39f; font-size:.85rem; margin-top:4px; }
  .price { color:#4ade80; font-weight:700; }
  .badge { background:#4ade80; color:#0f1a12; font-size:.7rem; font-weight:700;
           border-radius:4px; padding:1px 6px; margin-left:6px; }
  .empty { color:#9ab39f; padding:2rem 0; text-align:center; }
</style></head><body>
<h1>⛳ GolfWatch <span>· Waterloo, ON · {{radius}}km</span></h1>
<div class="sub">Keyword “{{keyword}}” · checked {{last_check}} · {{count}} listings tracked · page refreshes every 60s</div>
{% for l in listings %}
  <div class="card {{'new' if l.fresh else ''}}">
    <a class="title" href="{{l.url}}" target="_blank">{{l.title}}</a>
    {% if l.fresh %}<span class="badge">NEW</span>{% endif %}
    <div class="meta"><span class="price">{{l.price}}</span>
      · {{l.dist}} · first seen {{l.seen}}</div>
  </div>
{% else %}
  <div class="empty">Nothing yet — first poll runs on startup. Refresh in a minute.</div>
{% endfor %}
</body></html>
"""

_last_check = "starting…"


def poller():
    global _last_check
    conn = db_connect()
    blocklist = load_blocklist()
    seeded = conn.execute("SELECT COUNT(*) FROM seen_listings").fetchone()[0] > 0
    run_once(conn, blocklist, first_run=not seeded)
    _last_check = datetime.now(timezone.utc).strftime("%H:%M UTC")
    while True:
        time.sleep(POLL_SECONDS)
        try:
            run_once(conn, blocklist)
        except Exception as e:  # never let the poller die
            print(f"poller error: {e}", flush=True)
        _last_check = datetime.now(timezone.utc).strftime("%H:%M UTC")


@app.route("/")
def index():
    conn = db_connect()
    rows = conn.execute(
        "SELECT title, price, url, distance_km, seen_at FROM seen_listings "
        "ORDER BY seen_at DESC LIMIT 50"
    ).fetchall()
    conn.close()
    now = datetime.now(timezone.utc)
    listings = []
    for title, price, url, dist, seen_at in rows:
        try:
            seen_dt = datetime.fromisoformat(seen_at)
            age_min = (now - seen_dt).total_seconds() / 60
            seen_txt = seen_dt.strftime("%b %d %H:%M UTC")
        except (ValueError, TypeError):
            age_min, seen_txt = 9999, "?"
        listings.append({
            "title": title, "price": price, "url": url,
            "dist": f"{dist:.1f} km" if dist is not None else "distance unknown",
            "seen": seen_txt, "fresh": age_min < 30,
        })
    return render_template_string(
        PAGE, listings=listings, keyword=KEYWORD,
        radius=RADIUS_KM, last_check=_last_check, count=len(rows),
    )


threading.Thread(target=poller, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

