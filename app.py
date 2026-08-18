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

RSS_URL = (
    "https://www.kijiji.ca/rss-srp/b-search.html"
    f"?searchKeyword={KEYWORD}"
    f"&address=Waterloo%2C+ON"
    f"&ll={CENTER_LAT}%2C{CENTER_LNG}"
    f"&radius={RADIUS_KM}"
    "&sort=dateDesc"
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

GEORSS_NS = "{http://www.georss.org/georss}"
DC_NS = "{http://purl.org/dc/elements/1.1/}"


# ----------------- helpers -----------------
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
    conn.commit()
    return conn


def load_blocklist():
    try:
        with open(BLOCKLIST_PATH) as f:
            return [s.lower() for s in json.load(f)]
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def listing_id_from_url(url):
    # Kijiji URLs end in /<numeric id>
    m = re.search(r"/(\d{6,})(?:\?|$)", url)
    return m.group(1) if m else url


def parse_feed(xml_text):
    """Yield dicts: id, title, url, price, lat, lng, published."""
    root = ElementTree.fromstring(xml_text)
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        url = (item.findtext("link") or "").strip()
        desc = item.findtext("description") or ""
        pub = item.findtext("pubDate") or item.findtext(f"{DC_NS}date") or ""

        # price: Kijiji puts it in <g-core:price> sometimes, else in description
        price = ""
        for child in item:
            if child.tag.lower().endswith("price") and child.text:
                price = child.text.strip()
        if not price:
            m = re.search(r"\$[\d,]+(?:\.\d\d)?", desc)
            price = m.group(0) if m else "n/a"

        # coordinates via georss:point "lat lng"
        lat = lng = None
        point = item.findtext(f"{GEORSS_NS}point")
        if point:
            try:
                lat, lng = (float(x) for x in point.split())
            except ValueError:
                pass

        yield {
            "id": listing_id_from_url(url),
            "title": title,
            "url": url,
            "price": price,
            "lat": lat,
            "lng": lng,
            "published": pub,
        }


def notify(listing, dist_txt):
    line = f"⛳ NEW: {listing['title']} — {listing['price']} — {dist_txt} — {listing['url']}"
    print(line, flush=True)


# ----------------- main cycle -----------------
def run_once(conn, blocklist, first_run=False):
    try:
        resp = requests.get(RSS_URL, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[{datetime.now():%H:%M:%S}] fetch failed: {e}", flush=True)
        return

    new_count = 0
    for listing in parse_feed(resp.text):
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
