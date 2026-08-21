"""ResaleScout — multi-source local flip finder (Kijiji + Facebook Marketplace).

Sources
  kijiji : direct fetch, curl_cffi chrome impersonation, __NEXT_DATA__ parse
  fb     : Facebook Marketplace, via ONE of
           - a managed provider API   (FB_PROVIDER + FB_API_KEY)
           - an external browser agent POSTing to /ingest (INGEST_TOKEN)
           Direct fetch from this container is NOT attempted: Meta blocks
           datacenter ASNs on the first request.

Every listing, whatever the source, is normalised to the same dict and run
through the same AI resale valuation + deal flagging.
"""
import os, re, json, math, time, base64, sqlite3, threading, datetime
from urllib.parse import quote_plus
from flask import Flask, request, jsonify, render_template_string

try:
    from curl_cffi import requests as creq
except Exception:                       # local dev / offline
    creq = None
import requests as preq

# ---------------------------------------------------------------- config
KEYWORDS      = [k.strip() for k in os.environ.get(
                    "KEYWORDS", "golf").split(",") if k.strip()]

# Realistic KW-area secondhand ceilings by golf category. The model kept
# returning ~$300 for everything; these cap the damage when it over-reaches.
CATEGORY_CAPS = {
    "book": 15, "media": 15, "accessory": 30, "apparel": 40, "balls": 35,
    "shoes": 60, "gloves": 20, "training_aid": 70, "single_club": 120,
    "bag": 160, "putter": 250, "wedge": 150, "hybrid": 180, "fairway_wood": 200,
    "driver": 400, "iron_set": 500, "full_set": 800, "push_cart": 250,
    "electric_cart": 900, "launch_monitor": 900, "simulator": 1500,
    "rangefinder": 300, "other": 200,
}
# Brands that actually hold resale value; anything else gets the plain cap.
PREMIUM_BRANDS = ("titleist", "scotty cameron", "taylormade", "callaway", "ping",
                  "mizuno", "srixon", "cobra", "cleveland", "odyssey", "bettinardi",
                  "footjoy", "sun mountain", "clicgear", "bushnell", "garmin",
                  "trackman", "skytrak", "pxg", "vessel", "miura")
CENTER_LAT    = float(os.environ.get("CENTER_LAT", 43.4643))     # Waterloo, ON
CENTER_LNG    = float(os.environ.get("CENTER_LNG", -80.5204))
RADIUS_KM     = float(os.environ.get("RADIUS_KM", 40))
POLL_SECONDS  = int(os.environ.get("POLL_SECONDS", 300))
MAX_PRICE_CAD = float(os.environ.get("MAX_PRICE_CAD", 600))
MIN_MARGIN_PCT= float(os.environ.get("MIN_MARGIN_PCT", 50))
MIN_PROFIT_CAD= float(os.environ.get("MIN_PROFIT_CAD", 40))
# assume a polite offer below asking lands — profit is figured on that,
# not the sticker price. 10% is what reliably works locally; more only
# happens on stale listings.
HAGGLE_PCT    = min(max(float(os.environ.get("HAGGLE_PCT", 10)), 0), 50)
# listings below this predicted profit are noise: hidden from the dashboard
# and never pushed. Defaults to the deal floor.
SHOW_MIN_PROFIT = float(os.environ.get("SHOW_MIN_PROFIT_CAD", MIN_PROFIT_CAD))
KIJIJI_PAGES  = max(1, int(os.environ.get("KIJIJI_PAGES", 3)))

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
AI_MODEL      = os.environ.get("AI_MODEL", "deepseek/deepseek-chat")

# facebook
FB_PROVIDER   = os.environ.get("FB_PROVIDER", "").strip().lower()   # sociavault|scrapecreators|apify|custom
FB_API_KEY    = os.environ.get("FB_API_KEY", "").strip()
FB_ENDPOINT   = os.environ.get("FB_ENDPOINT", "").strip()           # for FB_PROVIDER=custom
FB_ACTOR      = os.environ.get("FB_ACTOR", "dtrungtin~facebook-marketplace-search")
FB_CITY       = os.environ.get("FB_CITY", "kitchener-waterloo")
INGEST_TOKEN  = os.environ.get("INGEST_TOKEN", "").strip()

# web push — notify on new listings for these keywords only
PUSH_KEYWORDS = [k.strip().lower() for k in os.environ.get("PUSH_KEYWORDS", "golf").split(",") if k.strip()]
PUSH_DEALS_ONLY = os.environ.get("PUSH_DEALS_ONLY", "0") == "1"
VAPID_PUBLIC  = os.environ.get("VAPID_PUBLIC", "")
VAPID_PRIVATE = os.environ.get("VAPID_PRIVATE", "")
VAPID_EMAIL   = os.environ.get("VAPID_EMAIL", "mailto:alerts@resalescout.local")

DB = os.environ.get("DB_PATH", "/tmp/resalescout.db")
_last_check = None
_source_status = {}

# ---------------------------------------------------------------- storage
def db():
    c = sqlite3.connect(DB, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")      # readers don't block the writer
    c.execute("PRAGMA busy_timeout=30000")    # wait instead of failing instantly
    c.execute("""CREATE TABLE IF NOT EXISTS listings(
        uid TEXT PRIMARY KEY, source TEXT, kw TEXT, title TEXT, price_num REAL,
        price TEXT, url TEXT, lat REAL, lng REAL, dist_km REAL, seen_at TEXT,
        est_cad REAL, margin_pct REAL, profit_cad REAL, deal INTEGER,
        confidence TEXT, speed TEXT, reason TEXT, category TEXT, brand TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS subs(
        endpoint TEXT PRIMARY KEY, sub TEXT, added TEXT)""")
    return c

def haversine(lat, lng):
    if lat is None or lng is None:
        return None
    r = 6371.0
    p1, p2 = math.radians(CENTER_LAT), math.radians(lat)
    dp, dl = p2 - p1, math.radians(lng - CENTER_LNG)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * r * math.asin(math.sqrt(a))

def norm_price(v, cents=False):
    """Accepts 500 (cents), '$123.45', 123.45 -> float dollars or None.
    Kijiji always sends integer cents, so the caller says so explicitly —
    guessing by magnitude turned a $5 listing into $500."""
    if v is None:
        return None
    if isinstance(v, dict):
        if "amount" in v or "value" in v:
            v = v.get("amount", v.get("value"))
        elif "amount_with_offset" in v:
            v, cents = v.get("amount_with_offset"), True
        else:
            return None
    if v is None:
        return None
    if isinstance(v, str):
        v = re.sub(r"[^\d.]", "", v)
        if not v:
            return None
        return float(v)                      # strings are already dollars
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v / 100.0 if cents else v

def mk(source, kw, title, price, url, lat=None, lng=None, uid=None, cents=False):
    p = norm_price(price, cents=cents)
    d = haversine(lat, lng)
    return {"uid": uid or f"{source}:{url}", "source": source, "kw": kw,
            "title": (title or "").strip(), "price_num": p,
            "price": f"${p:,.2f}" if p is not None else "—",
            "url": url, "lat": lat, "lng": lng, "dist_km": d}

# ---------------------------------------------------------------- kijiji
def kijiji_url(kw, page=1):
    slug = quote_plus(kw.replace(' ', '-'))
    tail = f"page-{page}/" if page > 1 else ""
    return ("https://www.kijiji.ca/b-buy-sell/kitchener-waterloo/"
            f"{slug}/{tail}k0c10l1700212")

def parse_next_data(html, kw, source="kijiji"):
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except Exception:
        return []
    out, stack = [], [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if node.get("title") and node.get("url") and "price" in node:
                loc = node.get("location") or {}
                co = loc.get("coordinates") or {}
                u = node["url"]
                out.append(mk(source, kw, node["title"], node.get("price"),
                              u if u.startswith("http") else "https://www.kijiji.ca" + u,
                              co.get("latitude"), co.get("longitude"),
                              uid=f"{source}:{node.get('id') or u}", cents=True))
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return out

def fetch_kijiji(kw):
    if creq is None:
        raise RuntimeError("curl_cffi unavailable")
    out, seen = [], set()
    for page in range(1, KIJIJI_PAGES + 1):
        r = creq.get(kijiji_url(kw, page), impersonate="chrome", timeout=30)
        got = [it for it in parse_next_data(r.text, kw) if it["uid"] not in seen]
        if not got and page > 1:      # past the last page
            break
        seen.update(it["uid"] for it in got)
        out.extend(got)
        if page < KIJIJI_PAGES:
            time.sleep(1.5)           # pace page pulls inside a cycle
    return out

# ---------------------------------------------------------------- facebook
def _fb_normalise(items, kw):
    """Map any provider's listing shape onto our dict."""
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        title = it.get("title") or it.get("marketplace_listing_title") or it.get("name")
        url   = it.get("url") or it.get("listingUrl") or it.get("link") or (
                f"https://www.facebook.com/marketplace/item/{it.get('id')}" if it.get("id") else None)
        if not title or not url:
            continue
        price = (it.get("price") or it.get("listing_price") or it.get("formatted_price")
                 or (it.get("amount") if "amount" in it else None))
        loc = it.get("location") or it.get("listing_location") or {}
        co  = loc.get("coordinates") or loc.get("reverse_geocode") or {}
        lat = co.get("latitude") or loc.get("latitude") or it.get("latitude")
        lng = co.get("longitude") or loc.get("longitude") or it.get("longitude")
        out.append(mk("facebook", kw, title, price, url,
                      lat, lng, uid=f"facebook:{it.get('id') or url}"))
    return out

def fetch_facebook(kw):
    """Managed-provider fetch. No provider configured -> no FB results (never
    a direct hit from this container; datacenter IPs are blocked on sight)."""
    if not FB_PROVIDER or not FB_API_KEY:
        raise RuntimeError("no FB provider configured")
    q = quote_plus(kw)
    if FB_PROVIDER == "sociavault":
        r = preq.get("https://api.sociavault.com/v1/facebook/marketplace/search",
                     headers={"x-api-key": FB_API_KEY},
                     params={"query": kw, "latitude": CENTER_LAT,
                             "longitude": CENTER_LNG, "radius": int(RADIUS_KM)},
                     timeout=45)
        r.raise_for_status()
        j = r.json()
        items = j.get("data", j).get("listings", j.get("data", []))
    elif FB_PROVIDER == "scrapecreators":
        r = preq.get("https://api.scrapecreators.com/v1/facebook/marketplace/search",
                     headers={"x-api-key": FB_API_KEY},
                     params={"query": kw, "lat": CENTER_LAT, "lng": CENTER_LNG,
                             "radius": int(RADIUS_KM)}, timeout=45)
        r.raise_for_status()
        j = r.json()
        items = j.get("listings", j.get("data", []))
    elif FB_PROVIDER == "apify":
        url = (f"https://api.apify.com/v2/acts/{FB_ACTOR}/run-sync-get-dataset-items"
               f"?token={FB_API_KEY}")
        payload = {"startUrls": [{"url": f"https://www.facebook.com/marketplace/"
                                         f"{FB_CITY}/search?query={q}&exact=false"}],
                   "maxItems": 40, "proxy": {"useApifyProxy": True,
                                             "apifyProxyGroups": ["RESIDENTIAL"]}}
        r = preq.post(url, json=payload, timeout=180)
        r.raise_for_status()
        j = r.json()
        items = j if isinstance(j, list) else j.get("items", [])
        if items and isinstance(items[0], dict) and kw in items[0]:
            items = items[0][kw]                       # actor groups by query
    elif FB_PROVIDER == "custom":
        r = preq.get(FB_ENDPOINT, headers={"Authorization": f"Bearer {FB_API_KEY}"},
                     params={"query": kw, "lat": CENTER_LAT, "lng": CENTER_LNG,
                             "radius": int(RADIUS_KM)}, timeout=60)
        r.raise_for_status()
        j = r.json()
        items = j.get("listings", j if isinstance(j, list) else j.get("data", []))
    else:
        raise RuntimeError(f"unknown FB_PROVIDER {FB_PROVIDER}")
    return _fb_normalise(items, kw)

SOURCES = {"kijiji": fetch_kijiji, "facebook": fetch_facebook}

# ---------------------------------------------------------------- AI valuation
JUNK_RE = re.compile(r"\b(book|encyclopedia|magazine|dvd|poster|calendar|card|"
                     r"sticker|tee time|lesson|membership|green fee|towel|"
                     r"keychain|trophy|ball marker|divot)\b", re.I)

def ai_value(title, price):
    if not OPENROUTER_API_KEY or price is None:
        return {}
    if JUNK_RE.search(title):                 # don't spend a call on $5 ephemera
        return {"category": "book", "est_resale_cad": min(price, 15),
                "confidence": "high", "flip": False, "brand": "",
                "reason": "printed/novelty item, negligible resale"}
    prompt = (
        f'You are a golf equipment reseller in Kitchener-Waterloo, Ontario. Price '
        f'this used listing for resale.\n\n'
        f'Listing: "{title}"\nAsking: CAD ${price:,.2f}\n\n'
        f'Classify it, then estimate what it realistically sells for used locally '
        f'within 30 days.\n'
        f'category must be one of: driver, fairway_wood, hybrid, iron_set, wedge, '
        f'putter, single_club, full_set, bag, push_cart, electric_cart, shoes, '
        f'apparel, gloves, balls, rangefinder, launch_monitor, simulator, '
        f'training_aid, accessory, book, media, other.\n\n'
        f'Typical used KW prices, for calibration:\n'
        f'- Used balls by the dozen $8-25; gloves/tees/towels under $20\n'
        f'- Beginner or unbranded single irons/woods $15-40\n'
        f'- Recent brand-name driver (last 5 yrs) $120-350; older $40-90\n'
        f'- Brand-name iron set (last 8 yrs) $150-450; old blades or beginner sets $60-150\n'
        f'- Scotty Cameron / Bettinardi putters $250-600; ordinary putters $25-80\n'
        f'- Stand/cart bags $40-140; Clicgear-type push carts $100-220\n'
        f'- Full beginner package sets $80-200; premium full sets $300-700\n'
        f'- Rangefinders $80-250; SkyTrak/launch monitors $400-900\n\n'
        f'Rules:\n'
        f'- Price the EXACT item named. Never assume a newer model, a complete set, '
        f'or accessories not stated.\n'
        f'- Unbranded, vintage, worn, or vague listings are near-worthless: estimate '
        f'at or below the asking price and set confidence low.\n'
        f'- Model year matters enormously. If no model is named, assume old and cheap.\n'
        f'- est_resale_cad is your CONSERVATIVE figure (what you are confident it '
        f'clears), est_high_cad your optimistic one. They must differ.\n'
        f'- flip is true only if you would personally buy this to resell.\n\n'
        f'Reply ONLY minified JSON, no markdown:\n'
        f'{{"category": "<one of the list>", "brand": "<brand or empty>", '
        f'"est_resale_cad": <number>, "est_high_cad": <number>, '
        f'"confidence": "high"|"medium"|"low", "speed": "fast"|"slow", '
        f'"flip": true|false, "reason": "<max 14 words, name the comparable>"}}')
    try:
        r = preq.post("https://openrouter.ai/api/v1/chat/completions",
                      headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
                      json={"model": AI_MODEL, "max_tokens": 300, "temperature": 0.3,
                            "messages": [{"role": "user", "content": prompt}]},
                      timeout=60)
        txt = r.json()["choices"][0]["message"]["content"]
        txt = re.sub(r"```(json)?", "", txt).strip()
        return json.loads(txt[txt.index("{"):txt.rindex("}") + 1])
    except Exception as e:
        print(f"ai error: {e}", flush=True)
        return {}

def cap_estimate(est, category, title, confidence):
    """Clamp the model's number to what the category can actually fetch here."""
    cap = CATEGORY_CAPS.get((category or "other").lower(), CATEGORY_CAPS["other"])
    # a brand premium is real on hard goods, not on worn shoes or used balls
    BRANDABLE = {"driver", "fairway_wood", "hybrid", "iron_set", "wedge", "putter",
                 "single_club", "full_set", "bag", "push_cart", "electric_cart",
                 "rangefinder", "launch_monitor", "simulator"}
    premium = (any(b in title.lower() for b in PREMIUM_BRANDS)
               and (category or "").lower() in BRANDABLE)
    if premium and confidence == "high":
        cap *= 1.5                      # a real Scotty can beat the generic ceiling
    return (min(est, cap), cap) if est else (est, cap)

def score(item):
    """AI value + deal maths. Mutates and returns item."""
    p = item.get("price_num")
    obj = ai_value(item["title"], p) if p is not None else {}
    est = obj.get("est_resale_cad")
    item["confidence"] = str(obj.get("confidence", "")).lower()
    item["speed"] = str(obj.get("speed", "")).lower()
    item["reason"] = obj.get("reason", "")
    item["category"] = str(obj.get("category", "")).lower()
    item["brand"] = obj.get("brand", "")
    # a resale estimate at or below zero is model garbage, not a valuation
    est = (float(est) if isinstance(est, (int, float))
           and math.isfinite(est) and est > 0 else None)
    est, cap = cap_estimate(est, item["category"], item["title"], item["confidence"])
    if est is not None and est != obj.get("est_resale_cad"):
        item["reason"] = (item["reason"] + f" · capped at {item['category']} ceiling")[:90]
    item["est_cad"] = est
    flip = obj.get("flip", True)
    if item["est_cad"] and p:
        buy = round(p * (1 - HAGGLE_PCT / 100.0), 2)   # expected price after haggling
        item["profit_cad"] = item["est_cad"] - buy
        item["margin_pct"] = (item["est_cad"] - buy) / buy * 100 if buy else None
        # dollars first: percentage on a cheap item is noise, not opportunity
        item["deal"] = int(bool(flip)
                           and item["profit_cad"] >= MIN_PROFIT_CAD
                           and item["margin_pct"] >= MIN_MARGIN_PCT
                           and item["confidence"] != "low")
    else:
        item["profit_cad"] = item["margin_pct"] = None
        item["deal"] = 0
    return item

def keep(item):
    if not item["title"]:
        return False
    if item["kw"].split()[0].lower() not in item["title"].lower():
        return False                                    # hard keyword filter
    if item["price_num"] is not None and item["price_num"] > MAX_PRICE_CAD:
        return False
    if item["dist_km"] is not None and item["dist_km"] > RADIUS_KM:
        return False
    return True

# ---------------------------------------------------------------- web push
def push_notify(item):
    """Fire a browser notification for a new listing. Keyword-gated."""
    if item["kw"].lower() not in PUSH_KEYWORDS:
        return
    if PUSH_DEALS_ONLY and not item["deal"]:
        return
    if item.get("profit_cad") is None or item["profit_cad"] < SHOW_MIN_PROFIT:
        return                      # not provably profitable — don't wake the phone
    if not (VAPID_PUBLIC and VAPID_PRIVATE):
        return
    try:
        from pywebpush import webpush, WebPushException
    except Exception:
        print("push: pywebpush not installed", flush=True)
        return
    body = item["price"]
    if item.get("est_cad"):
        body += f" → est ${item['est_cad']:,.0f}"
        if item.get("margin_pct") is not None:
            body += f" ({item['margin_pct']:.0f}%)"
    if item.get("dist_km") is not None:
        body += f" · {item['dist_km']:.0f} km"
    payload = json.dumps({"title": ("🔥 " if item["deal"] else "") + item["title"][:60],
                          "body": body, "url": item["url"], "tag": item["uid"]})
    con, dead = db(), []
    for endpoint, sub in con.execute("SELECT endpoint, sub FROM subs").fetchall():
        try:
            webpush(subscription_info=json.loads(sub), data=payload,
                    vapid_private_key=VAPID_PRIVATE,
                    vapid_claims={"sub": VAPID_EMAIL})
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (404, 410):
                dead.append(endpoint)          # subscription expired
            else:
                print(f"push error {code}: {e}", flush=True)
        except Exception as e:
            print(f"push error: {e}", flush=True)
    for endpoint in dead:
        con.execute("DELETE FROM subs WHERE endpoint=?", (endpoint,))
    con.commit(); con.close()


def save(items):
    """Score + persist unseen listings. Returns the new ones.

    The DB connection is NEVER held open across an AI call — DeepSeek takes
    seconds per listing, and holding the write lock that long made every other
    request (including /subscribe) fail with 'database is locked'."""
    # phase 1: short read to find what's genuinely new
    con = db()
    seen = {r[0] for r in con.execute("SELECT uid FROM listings")}
    con.close()

    candidates = [it for it in items if keep(it) and it["uid"] not in seen]

    # phase 2: the slow part, with no connection open
    for it in candidates:
        score(it)
        it["seen_at"] = datetime.datetime.now(datetime.UTC).isoformat()

    # phase 3: one short write
    fresh = []
    if candidates:
        con = db()
        for it in candidates:
            con.execute("""INSERT OR REPLACE INTO listings VALUES
                (:uid,:source,:kw,:title,:price_num,:price,:url,:lat,:lng,:dist_km,
                 :seen_at,:est_cad,:margin_pct,:profit_cad,:deal,:confidence,:speed,:reason,:category,:brand)""",
                {k: it.get(k) for k in ("uid source kw title price_num price url lat lng "
                                        "dist_km seen_at est_cad margin_pct profit_cad deal "
                                        "confidence speed reason category brand").split()})
            fresh.append(it)
        con.commit(); con.close()

    # phase 4: notify after the write is durable
    for it in fresh:
        try:
            push_notify(it)
        except Exception as e:
            print(f"push failed: {e}", flush=True)
        if it["deal"]:
            print(f"DEAL {it['source']} {it['title'][:60]} {it['price']} "
                  f"-> est ${it['est_cad']:,.0f} ({it['margin_pct']:.0f}%) {it['url']}",
                  flush=True)
    return fresh

# ---------------------------------------------------------------- poller
def poller():
    global _last_check
    while True:
        total = 0
        for name, fn in SOURCES.items():
            for kw in KEYWORDS:
                try:
                    got = fn(kw)
                    n = len(save(got))
                    total += n
                    _source_status[name] = "ok"
                    print(f"{name}/{kw}: {len(got)} parsed, {n} new", flush=True)
                except Exception as e:
                    _source_status[name] = f"off ({e})"
                    print(f"{name}/{kw}: {e}", flush=True)
                time.sleep(2)
        _last_check = datetime.datetime.now().strftime("%H:%M:%S")
        print(f"cycle done: {total} new listings", flush=True)
        time.sleep(POLL_SECONDS)

# ---------------------------------------------------------------- web
app = Flask(__name__)

@app.post("/ingest")
def ingest():
    """External browser agent (virtual PC) pushes listings here."""
    if not INGEST_TOKEN or request.headers.get("X-Ingest-Token") != INGEST_TOKEN:
        return jsonify(error="bad token"), 401
    body = request.get_json(force=True, silent=True) or {}
    raw = body.get("listings", body if isinstance(body, list) else [])
    kw = body.get("keyword", KEYWORDS[0]) if isinstance(body, dict) else KEYWORDS[0]
    items = _fb_normalise(raw, kw)
    new = save(items)
    _source_status["facebook"] = "ok (agent)"
    print(f"ingest: {len(items)} received, {len(new)} new", flush=True)
    return jsonify(received=len(items), new=len(new),
                   deals=[i["url"] for i in new if i["deal"]])

SW_JS = """self.addEventListener('push', e => {
  const d = e.data ? e.data.json() : {};
  e.waitUntil(self.registration.showNotification(d.title || 'New listing', {
    body: d.body || '', tag: d.tag, data: {url: d.url},
    icon: '/icon.png', badge: '/icon.png', vibrate: [120, 60, 120]
  }));
});
self.addEventListener('notificationclick', e => {
  e.notification.close();
  e.waitUntil(clients.openWindow(e.notification.data.url));
});"""

@app.get("/sw.js")
def sw():
    return SW_JS, 200, {"Content-Type": "application/javascript",
                        "Service-Worker-Allowed": "/"}

@app.get("/icon.png")
def icon():
    # 1x1 transparent png so the notification never shows a broken icon
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    ), 200, {"Content-Type": "image/png"}

@app.post("/subscribe")
def subscribe():
    sub = request.get_json(force=True, silent=True) or {}
    ep = sub.get("endpoint")
    if not ep:
        return jsonify(error="no endpoint"), 400
    con = db()
    con.execute("INSERT OR REPLACE INTO subs VALUES (?,?,?)",
                (ep, json.dumps(sub), datetime.datetime.now(datetime.UTC).isoformat()))
    con.commit(); con.close()
    print(f"push: subscribed {ep[:60]}", flush=True)
    return jsonify(ok=True)

@app.post("/test-push")
def test_push():
    push_notify({"kw": PUSH_KEYWORDS[0], "deal": 1, "title": "Test alert — ResaleScout",
                 "price": "$1", "url": "/", "uid": "test", "est_cad": None,
                 "dist_km": None, "margin_pct": None})
    con = db(); n = con.execute("SELECT COUNT(*) FROM subs").fetchone()[0]; con.close()
    return jsonify(sent_to=n)

PAGE = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>ResaleScout</title><style>
body{font:15px/1.45 -apple-system,system-ui,sans-serif;margin:0;background:#0f1115;color:#e8eaed}
header{padding:16px;border-bottom:1px solid #232733}h1{font-size:18px;margin:0 0 4px}
.sub{color:#8b93a7;font-size:12px}ul{list-style:none;margin:0;padding:8px}
li{padding:12px;border:1px solid #232733;border-radius:10px;margin:8px 0;background:#151823}
li.deal{border-color:#f5a524;background:#1d1a12}a{color:#e8eaed;text-decoration:none;font-weight:600}
.meta{color:#8b93a7;font-size:12px;margin-top:5px}.tag{font-size:11px;padding:2px 6px;border-radius:5px;
background:#232733;color:#a9b1c6;margin-right:5px}.fb{background:#1b3a5c;color:#8ec5ff}
.est{color:#4ade80;font-weight:600}.dim{color:#8b93a7}</style>
<header><h1>💰 ResaleScout</h1><div class=sub>{{count}} worth ≥${{minprofit}} · {{total}} scanned · {{deals}} 🔥 ·
{{keywords}} · under ${{maxprice}} · {{radius}}km · last {{last_check or '—'}}<br>
{% for s,v in status.items() %}<span class=tag>{{s}}: {{v[:40]}}</span>{% endfor %}<br>
<button id=nbtn>🔔 Alerts for {{pushkw}}</button></div></header>
<script>
const VAPID = "{{vapid}}";
function u8(b64){const p='='.repeat((4-b64.length%4)%4);
  const r=atob((b64+p).replace(/-/g,'+').replace(/_/g,'/'));
  return Uint8Array.from([...r].map(c=>c.charCodeAt(0)));}
const btn=document.getElementById('nbtn');
btn.onclick=async()=>{
  if(!VAPID){btn.textContent='push not configured';return;}
  try{
    const perm=await Notification.requestPermission();
    if(perm!=='granted'){btn.textContent='blocked in browser settings';return;}
    const reg=await navigator.serviceWorker.register('/sw.js');
    await navigator.serviceWorker.ready;
    let sub=await reg.pushManager.getSubscription();
    if(!sub) sub=await reg.pushManager.subscribe(
      {userVisibleOnly:true, applicationServerKey:u8(VAPID)});
    const r=await fetch('/subscribe',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(sub)});
    btn.textContent = r.ok ? '✅ alerts on' : 'failed, retry';
  }catch(e){btn.textContent='error: '+e.message;}
};
</script>
<ul>{% for l in listings %}<li class="{{'deal' if l.deal}}">
<a href="{{l.url}}" target=_blank>{{'🔥 ' if l.deal}}{{l.title}}</a>
<div class=meta><span class="tag {{'fb' if l.source=='facebook'}}">{{l.source}}</span>
<b>{{l.price}}</b>{% if l.profit %} · <span class=est>+${{l.profit}} profit</span>{% endif %}
{% if l.est %} <span class=dim>(est ${{l.est}}, {{l.margin}}%{% if l.confidence %},
{{l.confidence}} conf{% endif %}){% endif %}
{% if l.category %}<span class=tag>{{l.category}}</span>{% endif %}
{% if l.dist %} · {{l.dist}}{% endif %}</div>
{% if l.reason %}<div class=meta>{{l.reason}}</div>{% endif %}</li>{% endfor %}</ul>"""

@app.get("/")
def home():
    con = db()
    rows = con.execute("""SELECT * FROM listings WHERE profit_cad >= ?
                          ORDER BY deal DESC, profit_cad DESC, seen_at DESC
                          LIMIT 200""", (SHOW_MIN_PROFIT,)).fetchall()
    cols = [d[0] for d in con.execute("SELECT * FROM listings LIMIT 1").description]
    total = con.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    con.close()
    out = []
    for r in rows:
        d = dict(zip(cols, r))
        out.append({**d,
                    "est": f"{d['est_cad']:,.0f}" if d["est_cad"] else None,
                    "margin": f"{d['margin_pct']:.0f}" if d["margin_pct"] is not None else None,
                    "profit": f"{d['profit_cad']:,.0f}" if d["profit_cad"] and d["profit_cad"] > 0 else None,
                    "dist": f"{d['dist_km']:.1f} km" if d["dist_km"] is not None else None})
    return render_template_string(PAGE, listings=out, radius=int(RADIUS_KM),
        keywords=" · ".join(KEYWORDS), last_check=_last_check, count=len(out),
        deals=sum(1 for o in out if o["deal"]), maxprice=int(MAX_PRICE_CAD),
        status=_source_status, vapid=VAPID_PUBLIC, pushkw=" · ".join(PUSH_KEYWORDS),
        total=total, minprofit=int(SHOW_MIN_PROFIT))

@app.get("/health")
def health():
    return jsonify(ok=True, sources=_source_status, last_check=_last_check,
                   fb_provider=FB_PROVIDER or None, ingest=bool(INGEST_TOKEN))

threading.Thread(target=poller, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

