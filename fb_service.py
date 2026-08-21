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
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "1200"))   # 20 min ≈ human refresh
HEADLESS     = os.environ.get("HEADLESS", "1") == "1"

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


def cred_login(page):
    """Type the login form like a person would. Returns True on success."""
    page.goto("https://www.facebook.com/login", wait_until="domcontentloaded",
              timeout=90_000)
    time.sleep(random.uniform(2, 4))
    try:
        page.fill("#email", FB_EMAIL)
        time.sleep(random.uniform(0.6, 1.4))
        page.fill("#pass", FB_PASSWORD)
        time.sleep(random.uniform(0.6, 1.4))
        page.click("button[name=login]")
        page.wait_for_load_state("domcontentloaded", timeout=60_000)
        time.sleep(random.uniform(3, 6))
    except Exception as e:
        print(f"login attempt failed: {e}", flush=True)
        return False
    if "checkpoint" in page.url or "/login" in page.url:
        print(f"login CHALLENGED at {page.url[:110]} — log in once from a normal "
              f"browser, clear the challenge, then prefer FB_COOKIES", flush=True)
        return False
    print("facebook: logged in", flush=True)
    return True


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

        for _ in range(6):                   # slow, human-ish scroll
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
        elif FB_EMAIL and FB_PASSWORD:
            page = ctx.new_page()
            try:
                cred_login(page)
            finally:
                page.close()
        else:
            print("facebook: NO SESSION configured (FB_COOKIES or "
                  "FB_EMAIL/FB_PASSWORD) — expect login walls", flush=True)
        for kw in KEYWORDS:
            try:
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
        nap = POLL_SECONDS + random.randint(-60, 90)
        print(f"sleeping {nap}s", flush=True)
        time.sleep(max(120, nap))


if __name__ == "__main__":
    main()
