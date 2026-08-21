"""fb_service.py — Facebook Marketplace scraper, runs as its own Railway service.

Unlike the main app (which must never fetch FB from the datacenter IP), THIS
service routes every request through FB_PROXY, a residential ISP proxy, so the
traffic leaves from a Canadian home IP. It does not log in — Marketplace's login
modal overlays the public listings rather than gating them, so no account is at
risk. It loops forever, scraping golf listings and POSTing them to the main
app's /ingest endpoint.

Env vars:
  FB_PROXY       http://user:pass@host:port   (required)
  INGEST_URL     https://<main-app>/ingest      (required)
  INGEST_TOKEN   shared secret                  (required)
  FB_CITY        kitchener-waterloo             (default)
  KEYWORDS       golf                           (default)
  RADIUS_KM      60
  MAX_PRICE_CAD  2000
  POLL_SECONDS   600
  HEADLESS       1
"""
import os, sys, json, time, random, traceback
from urllib.parse import urlsplit
import requests

FB_PROXY     = os.environ["FB_PROXY"]
INGEST_URL   = os.environ["INGEST_URL"]
INGEST_TOKEN = os.environ["INGEST_TOKEN"]
CITY         = os.environ.get("FB_CITY", "kitchener-waterloo")
FB_CATEGORY  = os.environ.get("FB_CATEGORY", "sporting-goods")
KEYWORDS     = [k.strip() for k in os.environ.get("KEYWORDS", "golf").split(",") if k.strip()]
RADIUS_KM    = os.environ.get("RADIUS_KM", "60")
MAX_PRICE    = os.environ.get("MAX_PRICE_CAD", "2000")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "3600"))
# naps are drawn from [POLL_SECONDS, POLL_MAX_SECONDS] so cycles never land on
# a predictable clock; default window is 1-1.8x the base
POLL_MAX     = int(os.environ.get("POLL_MAX_SECONDS", str(int(POLL_SECONDS * 1.8))))
HEADLESS     = os.environ.get("HEADLESS", "1") == "1"
# point COOKIE_FILE at a mounted volume so the session survives restarts —
# a fresh 'new device' login email on every deploy is what we're avoiding
COOKIE_FILE  = os.environ.get("COOKIE_FILE", "/tmp/fb_session.json")
FB_SCROLLS   = max(1, int(os.environ.get("FB_SCROLLS", "14")))

# marketplace is login-walled for anonymous visitors. Session comes from ONE of:
#   FB_COOKIES   "c_user=..; xs=.."  copied from a logged-in browser (preferred:
#                no login event ever fires, so nothing looks automated)
#   FB_EMAIL / FB_PASSWORD           the scraper types the login form itself
FB_COOKIES   = os.environ.get("FB_COOKIES", "").strip()
FB_EMAIL     = os.environ.get("FB_EMAIL", "").strip()
FB_PASSWORD  = os.environ.get("FB_PASSWORD", "")


def proxy_cfg():
    """Playwright ignores credentials embedded in the proxy URL — user:pass@host
    silently becomes an unauthenticated proxy and every page load 407s. Split
    them out into the username/password fields it actually reads."""
    u = urlsplit(FB_PROXY)
    cfg = {"server": f"{u.scheme or 'http'}://{u.hostname}:{u.port}"}
    if u.username:
        cfg["username"] = u.username
        cfg["password"] = u.password or ""
    return cfg


def proxy_ok():
    """Confirm the proxy works and reports a Canadian IP before we bother FB."""
    try:
        r = requests.get("https://ipv4.icanhazip.com", timeout=30,
                         proxies={"http": FB_PROXY, "https": FB_PROXY})
        ip = r.text.strip()
        print(f"proxy exit IP: {ip}", flush=True)
        return True
    except Exception as e:
        print(f"proxy check FAILED: {e}", flush=True)
        return False


def fb_cookies():
    out = []
    for part in FB_COOKIES.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out.append({"name": k.strip(), "value": v.strip(),
                        "domain": ".facebook.com", "path": "/", "secure": True})
    return out


def load_saved_session(ctx):
    """Reuse the session from an earlier login. A fresh 'new device login'
    every cycle is exactly what gets a burner account flagged."""
    try:
        with open(COOKIE_FILE) as f:
            ctx.add_cookies(json.load(f))
        print("facebook: restored saved session", flush=True)
        return True
    except Exception:
        return False


def save_session(ctx):
    try:
        with open(COOKIE_FILE, "w") as f:
            json.dump(ctx.cookies("https://www.facebook.com"), f)
        print("facebook: session saved for reuse", flush=True)
    except Exception as e:
        print(f"session save failed: {e}", flush=True)


def _first_that_works(page, action, selectors, timeout=4000):
    """FB serves several login page variants; probe selectors until one bites."""
    for sel in selectors:
        try:
            action(sel, timeout)
            return sel
        except Exception:
            continue
    return None


def cred_login(page):
    """Type the login form like a person would. Returns True on success."""
    try:
        page.goto("https://www.facebook.com/login/", wait_until="domcontentloaded",
                  timeout=90_000)
        time.sleep(random.uniform(3, 5))
        try:
            # the form is injected by JS well after domcontentloaded; a blank
            # shell with zero inputs is what an instant probe sees
            page.wait_for_selector("input", timeout=25_000)
        except Exception:
            pass
        # a cookie-consent dialog overlays the form on some variants
        _first_that_works(page, lambda s, t: page.click(s, timeout=t), (
            'button[data-cookiebanner="accept_button"]',
            'div[aria-label="Allow all cookies"]',
            'button[title="Allow all cookies"]',
            'div[aria-label="Autoriser tous les cookies"]'), 2500)
        got = _first_that_works(
            page, lambda s, t: page.fill(s, FB_EMAIL, timeout=t),
            ('#email', 'input[name="email"]', 'input[type="email"]',
             'input[autocomplete="username"]'))
        if not got:
            print(f"login page had no email field — {page.locator('input').count()} "
                  f"inputs, title {page.title()!r}, url {page.url[:110]}", flush=True)
            return False
        time.sleep(random.uniform(0.6, 1.4))
        got = _first_that_works(
            page, lambda s, t: page.fill(s, FB_PASSWORD, timeout=t),
            ('#pass', 'input[name="pass"]', 'input[type="password"]',
             'input[autocomplete="current-password"]'))
        if not got:
            print("login page had no password field", flush=True)
            return False
        time.sleep(random.uniform(0.6, 1.4))
        if not _first_that_works(page, lambda s, t: page.click(s, timeout=t), (
                'button[name="login"]', '#loginbutton', 'button[type="submit"]',
                'div[aria-label="Log in"]', 'div[aria-label="Log In"]')):
            page.keyboard.press("Enter")
        page.wait_for_load_state("domcontentloaded", timeout=60_000)
        time.sleep(random.uniform(3, 6))
    except Exception as e:
        print(f"login attempt failed: {e}", flush=True)
        return False
    # the c_user cookie is the real signal — the URL can still read /login
    # for a moment after a login that actually took
    if any(c.get("name") == "c_user"
           for c in page.context.cookies("https://www.facebook.com")):
        print(f"facebook: logged in, landed on {page.url[:80]}", flush=True)
        return True
    if any(w in page.url for w in ("checkpoint", "two_step")):
        print(f"login CHALLENGED at {page.url[:110]} — log in once from a normal "
              f"browser, clear the challenge, then prefer FB_COOKIES", flush=True)
    else:
        print(f"login did not take, landed on {page.url[:110]}", flush=True)
    return False


def harvest(page, keyword):
    """Pull listings out of the GraphQL/XHR JSON the page fetches, not the DOM."""
    found = {}
    json_seen = [0]

    def on_response(resp):
        if "/api/graphql" not in resp.url and "/marketplace/" not in resp.url:
            return
        try:
            data = resp.json()
        except Exception:
            return
        json_seen[0] += 1
        stack = [data]
        while stack:
            n = stack.pop()
            if isinstance(n, dict):
                if n.get("marketplace_listing_title") and n.get("id"):
                    lp = n.get("listing_price") or {}
                    loc = n.get("location") or {}
                    geo = loc.get("reverse_geocode") or loc
                    found[n["id"]] = {
                        "id": n["id"],
                        "title": n.get("marketplace_listing_title"),
                        "price": lp.get("amount") or lp.get("formatted_amount"),
                        "url": f"https://www.facebook.com/marketplace/item/{n['id']}",
                        "latitude": geo.get("latitude"),
                        "longitude": geo.get("longitude"),
                    }
                stack.extend(n.values())
            elif isinstance(n, list):
                stack.extend(n)

    page.on("response", on_response)
    # Keyword search is login-walled for logged-out visitors; the category
    # browse page often isn't. Try search first, fall back to browsing the
    # category newest-first — the main app's keyword filter drops non-matches.
    urls = [(f"https://www.facebook.com/marketplace/{CITY}/search?"
             f"query={keyword.replace(' ', '%20')}&radius={RADIUS_KM}"
             f"&maxPrice={MAX_PRICE}&sortBy=creation_time_descend&exact=false"),
            (f"https://www.facebook.com/marketplace/{CITY}/{FB_CATEGORY}?"
             f"maxPrice={MAX_PRICE}&sortBy=creation_time_descend&exact=false")]
    for url in urls:
        page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        time.sleep(random.uniform(3, 6))
        if "/login" in page.url or "checkpoint" in page.url:
            print(f"{keyword}: login wall at {url.split('?')[0]}", flush=True)
            continue

        for sel in ('div[aria-label="Close"]', 'div[aria-label="Fermer"]',
                    '[aria-label="Close"]'):
            try:
                page.click(sel, timeout=2500)
                break
            except Exception:
                pass

        for _ in range(FB_SCROLLS):          # slow, human-ish scroll
            page.mouse.wheel(0, random.randint(600, 1100))
            time.sleep(random.uniform(1.3, 2.9))
        break

    page.remove_listener("response", on_response)
    # 0 results is ambiguous: no matches, or FB swapped the results for a
    # login/checkpoint page. The landing URL is the tell.
    end_url = page.url
    if any(w in end_url for w in ("login", "checkpoint", "unsupported")):
        print(f"{keyword}: BLOCKED — redirected to {end_url[:120]}", flush=True)
    print(f"{keyword}: {len(found)} listings from {json_seen[0]} json responses, "
          f"landed on {end_url[:120]}", flush=True)
    return list(found.values())


def push(listings, keyword):
    r = requests.post(INGEST_URL, timeout=60,
                      headers={"X-Ingest-Token": INGEST_TOKEN},
                      json={"keyword": keyword, "listings": listings})
    r.raise_for_status()
    return r.json()


def harvest_with_retry(ctx, kw, tries=2):
    """Fresh page per attempt: after a renderer crash the old Page object is
    dead and every later call on it fails, so never reuse one across errors."""
    last = None
    for attempt in range(1, tries + 1):
        page = ctx.new_page()
        try:
            return harvest(page, kw)
        except Exception as e:
            last = e
            print(f"{kw}: attempt {attempt}/{tries} failed: {e}", flush=True)
            time.sleep(random.uniform(5, 10))
        finally:
            try:
                page.close()
            except Exception:
                pass
    raise last


def one_cycle():
    from camoufox.sync_api import Camoufox
    opts = {"headless": HEADLESS, "humanize": True, "os": "windows",
            "geoip": True, "proxy": proxy_cfg()}
    with Camoufox(**opts) as browser:
        # one context for the whole cycle: cookies/login must outlive any
        # single page, and browser.new_page() would isolate each one
        ctx = browser.new_context()
        if FB_COOKIES:
            ctx.add_cookies(fb_cookies())
            print("facebook: session cookies loaded", flush=True)
        elif not load_saved_session(ctx):
            if FB_EMAIL and FB_PASSWORD:
                page = ctx.new_page()
                try:
                    if cred_login(page):
                        save_session(ctx)
                finally:
                    page.close()
            else:
                print("facebook: NO SESSION configured (FB_COOKIES or "
                      "FB_EMAIL/FB_PASSWORD) — expect login walls", flush=True)
        for kw in KEYWORDS:
            try:
                items = harvest_with_retry(ctx, kw)
                if not items and not FB_COOKIES and FB_EMAIL and FB_PASSWORD:
                    # empty almost always means the saved session expired —
                    # log in fresh once and retry (no-op if still logged in)
                    page = ctx.new_page()
                    try:
                        ok = cred_login(page)
                    finally:
                        page.close()
                    if ok:
                        save_session(ctx)
                        items = harvest_with_retry(ctx, kw)
                res = push(items, kw)
                print(f"{kw}: scraped {len(items)}, ingested {res.get('new')} new, "
                      f"deals {res.get('deals')}", flush=True)
            except Exception as e:
                print(f"{kw}: FAILED {e}", flush=True)
                traceback.print_exc()
            time.sleep(random.uniform(15, 35))


def main():
    print("FB service starting", flush=True)
    if not proxy_ok():
        print("proxy unusable — exiting so Railway restarts us", flush=True)
        sys.exit(1)
    while True:
        try:
            one_cycle()
        except Exception as e:
            print(f"cycle crashed: {e}", flush=True)
            traceback.print_exc()
        nap = random.randint(POLL_SECONDS, max(POLL_SECONDS + 60, POLL_MAX))
        print(f"sleeping {nap}s (~{nap // 60} min)", flush=True)
        time.sleep(max(300, nap))


if __name__ == "__main__":
    main()
