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
KEYWORDS     = [k.strip() for k in os.environ.get("KEYWORDS", "golf").split(",") if k.strip()]
RADIUS_KM    = os.environ.get("RADIUS_KM", "60")
MAX_PRICE    = os.environ.get("MAX_PRICE_CAD", "2000")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "600"))
HEADLESS     = os.environ.get("HEADLESS", "1") == "1"


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


def harvest(page, keyword):
    """Pull listings out of the GraphQL/XHR JSON the page fetches, not the DOM."""
    found = {}

    def on_response(resp):
        if "/api/graphql" not in resp.url and "/marketplace/" not in resp.url:
            return
        try:
            data = resp.json()
        except Exception:
            return
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
    url = (f"https://www.facebook.com/marketplace/{CITY}/search?"
           f"query={keyword.replace(' ', '%20')}&radius={RADIUS_KM}"
           f"&maxPrice={MAX_PRICE}&sortBy=creation_time_descend&exact=false")
    page.goto(url, wait_until="domcontentloaded", timeout=90_000)
    time.sleep(random.uniform(3, 6))

    for sel in ('div[aria-label="Close"]', 'div[aria-label="Fermer"]',
                '[aria-label="Close"]'):
        try:
            page.click(sel, timeout=2500)
            break
        except Exception:
            pass

    for _ in range(6):                       # slow, human-ish scroll
        page.mouse.wheel(0, random.randint(600, 1100))
        time.sleep(random.uniform(1.3, 2.9))

    page.remove_listener("response", on_response)
    return list(found.values())


def push(listings, keyword):
    r = requests.post(INGEST_URL, timeout=60,
                      headers={"X-Ingest-Token": INGEST_TOKEN},
                      json={"keyword": keyword, "listings": listings})
    r.raise_for_status()
    return r.json()


def harvest_with_retry(browser, kw, tries=2):
    """Fresh page per attempt: after a renderer crash the old Page object is
    dead and every later call on it fails, so never reuse one across errors."""
    last = None
    for attempt in range(1, tries + 1):
        page = browser.new_page()
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
        for kw in KEYWORDS:
            try:
                items = harvest_with_retry(browser, kw)
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
