# Fetching Kijiji.ca Search Results from a Datacenter Host in 2025–2026: What Actually Works

## TL;DR
- **Kijiji's RSS feeds are officially dead** (the `rss-srp/b-search.html` path and all RSS was phased out by Kijiji in 2024), and the **old anonymous mobile API is broken**, so your only viable route is to fetch the **regular HTML search page (`/b-search.html`) and parse the embedded `__NEXT_DATA__` JSON** — Kijiji is a Next.js site and ships all listing data as JSON inside every page.
- **Your 403 is a fingerprint/behavior block, not a hard datacenter-IP ban**: switch from `requests` to **`curl_cffi` with `impersonate="chrome"`** (fixes the TLS/JA3 + HTTP/2 mismatch that causes an instant 403), send a full browser header set, persist cookies, and space requests out. A single clean request from a datacenter IP generally succeeds; blocking is cookie/session-and-volume based.
- For a hobby POC doing **one search every 2 minutes (~720/day, ~21,600/month)**, start **zero-cost** with `curl_cffi` from Railway; if you still get blocked, add a cheap Canadian residential proxy. Managed scraping APIs (ZenRows, ScrapingBee, ScraperAPI, Scrapfly) all have free tiers but they cap at ~1,000–5,000 requests/month — far below your volume — so they are a fallback for reliability, not the primary plan.

## Key Findings

### 1. Anti-bot setup: not the vendor you'd expect
Contrary to the assumption that Kijiji runs Cloudflare/DataDome/Akamai, there is **no primary-source evidence of any of the four big JS-challenge vendors** on kijiji.ca — no `cf-ray`/`__cf_bm` (Cloudflare), no `_abck`/`akamai-grn` (Akamai), no `reese84`/`visid_incap` (Imperva), no `x-datadome` (DataDome). The strongest concrete data point is a documented block page: when a scraper trips Kijiji's detection, kijiji.ca returns a canonical **Amazon S3 / CloudFront `<Error><Code>AccessDenied</Code>...` XML page**, and the reporter noted *"Once I clear my cache, and cookies, I'm able to access the website again."* [GitHub](https://github.com/mwpenny/kijiji-scraper/issues/36) This points to an **AWS-fronted stack (S3/CloudFront + likely AWS WAF or a first-party bot layer)** with **cookie/session-and-behavior-based blocking**, not a pure datacenter-IP ban. The authenticated mobile-API login path additionally carries an `x-threatmetrix-session-id` header (LexisNexis ThreatMetrix device fingerprinting).

**Is TLS fingerprinting involved?** Yes, at least enough that plain `python requests` fails where a browser succeeds. `requests`/`urllib3` produce a JA3/JA4 TLS fingerprint and HTTP/1.1 handshake that look nothing like Chrome; many anti-bot layers reject this before any HTML is served, [Scrapfly](https://scrapfly.io/blog/posts/403-forbidden-web-scraping) which is the classic "works in browser, 403 in Python" signature you're hitting. [ScrapingBee](https://www.scrapingbee.com/blog/web-scraping-without-getting-blocked/) This is exactly what `curl_cffi`/`curl-impersonate` fixes.

**Do they block datacenter IPs outright?** No, not outright. A single well-formed request to a listing/search page succeeds even from cloud infrastructure. But sustained automated access from one IP escalates to blocks, and commercial Kijiji scrapers default to **Canadian residential IPs** for reliability at volume.

### 2. RSS feeds are gone; `rss-srp/b-search.html` is deprecated
Kijiji **officially discontinued RSS**. Kijiji's own help desk now states they are "phasing out the RSS feed option, and it is no longer available." Multiple users confirmed the feeds stopped working in 2024. Your URL `https://www.kijiji.ca/rss-srp/b-search.html?...` is a dead path — there is **no current working RSS URL format**. The older `rss-` prefix trick (e.g. `www.kijiji.ca/rss-b-baby-stroller.../...`) [IFTTT](https://ifttt.com/applets/AMH3cUvt-kijiji) is also dead.

**What still works:** the regular HTML search endpoint, `https://www.kijiji.ca/b-search.html`, [npm](https://www.npmjs.com/package/kijiji-scraper) with your same query parameters. Example equivalent of your search:
`https://www.kijiji.ca/b-search.html?searchKeyword=golf&address=Waterloo%2C+ON&ll=43.4643%2C-80.5204&radius=40&sort=dateDesc`
`sort=dateDesc` (newest first) is still a valid sort parameter. This returns a full Next.js HTML page.

### 3. Programmatic access: the mobile API is half-dead; scrapers now parse HTML
- **Old mobile API (`mingle.kijiji.ca` / historically "api.kijiji.ca" / "Anapi"):** The architecture is alive but **anonymous search via the API is broken**. The most-referenced library, `mwpenny/kijiji-scraper` (npm), explicitly marks its `scraperType: "api"` mode as broken — verbatim from its README: *"'html' to scrape the website (default) and 'api' to use the mobile API (currently broken). If you have trouble with one, try the other."* The API uses eBay-Classifieds-Group `x-ecg-*` headers, an iOS user-agent, and returns XML. The embedded Basic-auth credential historically used to reach the API before login is `bm9uZTpwYXNz`, which decodes to `none:pass`.
- **Authenticated API still works:** `jackm/kijiji-manager` (Python, on PyPI, maintained through 2024+) is "Completely API driven, with no web scraping" [GitHub](https://github.com/jackm/kijiji-manager/blob/master/README.md) and can still log in, post, repost, and delete ads via `mingle.kijiji.ca` — but that requires a real Kijiji account and is for managing your own ads, not anonymous search.
- **Actively-maintained open-source projects:** `mwpenny/kijiji-scraper` (Node/TypeScript) is the reference implementation and is **more current than it first appears** — npm shows **v6.3.7 published ~3 months before August 2026**, with open issues as recent as December 17, 2024 (#78). It still does HTML scraping (its `api` mode is the broken part) and periodically needs fixes when Kijiji changes markup. `JGBMichalski/Kijiji-Scraper` (Python, Docker image available) is a maintained continuation of the older `CRutkowski/Kijiji-Scraper` and does HTML scraping + email alerts. [GitHub](https://github.com/JGBMichalski/Kijiji-Scraper) `jackm/kijiji-manager` is the most actively maintained but is for account/ad management, not search. Numerous paid Apify actors exist (e.g. `automation-lab/kijiji-scraper`, `fayoussef/kijiji-scraper` [Apify](https://apify.com/fayoussef/kijiji-scraper/api) at ~$0.9/1K), most of which default to Canadian residential proxies.

### 4. Practical workarounds from a datacenter host
- **`curl_cffi` / TLS-impersonation libraries:** This is the single highest-leverage fix. `curl_cffi` (Python binding to curl-impersonate) replays Chrome's exact TLS ClientHello, cipher order, extensions, and HTTP/2 SETTINGS frame. [Bright Data](https://brightdata.com/blog/web-data/web-scraping-with-curl-cffi) Swap `import requests` for `from curl_cffi import requests` and add `impersonate="chrome"`. [Datahut](https://www.blog.datahut.co/post/web-scraping-without-getting-blocked-curl-cffi) As of August 2026 the current release is **`curl_cffi` v0.15.0** (Python 3.10+ minimum since v0.14), which supports browser profiles through `chrome146`/`safari2601` and added HTTP/3 fingerprints and UDP SOCKS5 proxy support — use the bare `"chrome"` alias to always get the latest profile. `tls-client` is an alternative. For a TLS/first-party-fingerprint layer like Kijiji's (no heavy JS challenge on listing pages), TLS impersonation from a clean IP has a good chance of returning 200.
- **Simple header changes alone:** Insufficient by themselves against a TLS-fingerprinting layer — a Chrome User-Agent on a Python TLS stack is a mismatch that gets flagged. [Scrapfly](https://scrapfly.io/blog/posts/how-to-bypass-cloudflare-anti-scraping) But headers matter *in combination* with `curl_cffi`: send `Accept`, `Accept-Language: en-CA`, `Accept-Encoding`, and the `Sec-Fetch-*`/`sec-ch-ua` set, in browser order. [Scrapfly](https://scrapfly.io/blog/posts/403-forbidden-web-scraping)
- **Are residential/mobile proxies required?** Not for low-volume single requests, but **recommended for sustained polling**. Because Kijiji's block is session/behavior-based and geo-relevant (Kijiji is Canada-only), a Canadian residential IP is the most reliable. Commercial actors default to Canadian residential proxies.
- **Cheapest reliable options with free tiers:** Managed scraping APIs bundle residential proxies + fingerprinting behind one call. Free tiers as of 2026:
  - **ZenRows** — the largest and only truly recurring free allotment; ZenRows states *"It's recurring, not a time-limited trial: 5,000 credits every month, no credit card required."*
  - **ScrapingBee** — a **one-time** (non-recurring) trial: *"Try ScrapingBee with 1,000 free API credits… No credit card required."*
  - **ScraperAPI** — *"1,000 free API credits per month (with a maximum of 5 concurrent connections)… For the first 7-days after you sign up you will have access to 5,000 free requests."*
  - **Scrapfly** and **Firecrawl** — 1,000 free credits each.
  - **Bright Data** advertises a Kijiji-specific scraper with up to 5,000 page loads/month free. [Bright Data](https://brightdata.com/products/web-scraper/kijiji)

  Note the credit math: protected-site requests consume multiple credits each, and at ~21,600 requests/month your volume exceeds every free tier, so free API plans are a testing/fallback tool, not the steady-state solution.

### 5. Official / semi-official structured alternatives
- **Kijiji Search Alerts (email/push):** Kijiji still offers free "Search Alerts" that email you when new ads match a saved search. This is the closest thing to an official notification feed. You could subscribe an inbox to a saved "golf near Waterloo, newest" alert and parse the alert emails via IMAP. Caveat: alert cadence is not real-time (Kijiji describes daily digests), so this won't hit your 2-minute freshness goal, but it's fully sanctioned and zero-infrastructure.
- **Embedded JSON (`__NEXT_DATA__`) — the best structured target:** Kijiji is built on **Next.js (Pages Router)**, confirmed by `next-head-count` meta tags and `/next-assets/` paths in live markup. That means every search/listing page serializes its data into a `<script id="__NEXT_DATA__" type="application/json">…</script>` blob containing `props.pageProps` — clean structured listing data (title, price, location, coordinates, posting date, ad URL). Fetch the HTML once, extract that one script tag, and `json.loads` it — far more robust than CSS/HTML scraping, which breaks when Kijiji changes class names. No public GraphQL endpoint is exposed to the anonymous web front end. (Listing images resolve through a first-party AWS-backed media API, `media.kijiji.ca/api/v1/...`.)

## Details: recommended implementation

**Primary approach (zero-cost):** From Railway, use `curl_cffi` to GET the `/b-search.html` URL with `impersonate="chrome"`, a persistent session (to carry cookies), `Accept-Language: en-CA`, and a randomized delay around your 2-minute interval. Parse the `__NEXT_DATA__` JSON. Deduplicate by ad ID so you only surface new listings.

```python
from curl_cffi import requests  # curl_cffi >= 0.15.0
import json, re

URL = ("https://www.kijiji.ca/b-search.html?searchKeyword=golf"
       "&address=Waterloo%2C+ON&ll=43.4643%2C-80.5204&radius=40&sort=dateDesc")

s = requests.Session()  # persists cookies across polls
r = s.get(URL, impersonate="chrome", headers={
    "Accept-Language": "en-CA,en;q=0.9",
    "Referer": "https://www.kijiji.ca/",
})
r.raise_for_status()

m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
              r.text, re.S)
data = json.loads(m.group(1))
# listings live under data["props"]["pageProps"] — inspect once to map exact keys
```

**If that returns 403/AccessDenied at your polling rate:** (a) slow down / add jitter and confirm cookies persist across calls; (b) route through one cheap Canadian residential proxy (curl_cffi supports `proxies=`); (c) as a reliability fallback, send the URL through a managed API's free tier.

## Recommendations (ranked: zero-cost & simplest first, then reliability)

1. **Start here — `curl_cffi` + `__NEXT_DATA__` parsing, direct from Railway (cost: $0).** Highest simplicity, no external dependencies, and it directly addresses the root cause of your 403 (TLS fingerprint). Benchmark: if you get 200s for a few hours at 2-minute intervals, you're done. **Threshold to escalate:** if you see recurring 403s or the AWS `AccessDenied` XML after N requests, move to step 2.
2. **Add a Canadian residential proxy (cost: low, ~a few $/GB).** Keep the same `curl_cffi` code, add `proxies=`. HTML pages are small so bandwidth cost is minimal. Use a sticky session so your cookie/IP pairing stays consistent. **Threshold:** if blocks persist or you don't want to manage proxies, go to step 3.
3. **Managed scraping API free tier as fallback (cost: $0 within free tier).** ZenRows (largest, recurring 5,000 credits/month free) or ScrapingBee/Scrapfly — send the `/b-search.html` URL, let them handle proxies + fingerprinting, and parse `__NEXT_DATA__` from the returned HTML. **Watch the credit ceiling:** your ~21,600 req/month exceeds all free tiers, so use this only to prove reliability or for a lower polling rate. If you need a paid plan full-time, the cheapest tiers start at **$49/month** — ScraperAPI's $49 "Hobby" plan, for example, includes ~100,000 API credits, which comfortably covers 21,600 basic requests/month (Decodo starts lower at ~$29/month if you only need proxies).
4. **Parallel safety net — Kijiji Search Alerts by email (cost: $0, fully sanctioned).** Set up a saved-search alert and parse the emails. Not real-time, but it's the official channel and a good backstop if scraping access degrades.

**What would change these recommendations:** If Kijiji deploys a full JS-challenge WAF (you'd start seeing branded Cloudflare/DataDome interstitials or `cf-ray`/`x-datadome` headers), `curl_cffi` alone would stop working and you'd need a headless browser (Playwright/Nodriver) or a managed API. If your use grows beyond hobby scale, jump to a paid API plan or a Canadian residential proxy pool.

## Caveats
- **Legal/ToS:** Kijiji's Terms of Use restrict automated access; [Kijiji Canada](https://help.kijiji.ca/helpdesk/basics/kijiji-terms-of-use) this report describes technical feasibility for a personal POC, not legal advice. Keep volume polite (your 2-minute interval is reasonable), identify yourself where practical, and stop if asked.
- **The exact current WAF vendor is unconfirmed.** Evidence points to an AWS-fronted/first-party layer rather than Cloudflare/DataDome/Akamai/Imperva, but no live 2026 header dump was obtained. Run `curl -sI https://www.kijiji.ca/` and inspect cookies/headers (look for `aws-waf-token`, `x-amzn-waf-*`, or any vendor cookie) to confirm before investing in a specific bypass.
- **`__NEXT_DATA__` key paths change.** Inspect the JSON structure once to map where listings live under `props.pageProps`; Next.js buildId changes won't break you, but a page-props refactor might.
- **Free-tier figures move.** Scraping-API free allotments and credit multipliers change frequently (and JS-rendering/premium-proxy multipliers can quietly 5–75× your consumption); verify current limits at signup.
- **The mobile-API `none:pass` credential and anonymous-search behavior can change without notice** — treat the mobile API as unreliable for anonymous search.
