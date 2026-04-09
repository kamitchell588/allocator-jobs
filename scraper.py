#!/usr/bin/env python3
"""
Institutional Allocator Job Scraper
Pulls jobs from Apify dataset + supplemental sources and filters for
institutional allocator roles.
"""

import requests
import csv
import json
import os
import re
import sys
from datetime import datetime, timezone
from html import escape

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("beautifulsoup4 not installed — run: pip install beautifulsoup4 lxml")
    sys.exit(1)

# ── Configuration ────────────────────────────────────────────────────────────
# Token is read from .env file (never committed to git)
import pathlib, re as _re
_env = pathlib.Path(__file__).parent / ".env"
_env_vars = dict(_re.findall(r'^([A-Z_]+)=(.+)$', _env.read_text(), _re.M)) if _env.exists() else {}
APIFY_TOKEN = _env_vars.get("APIFY_TOKEN", os.environ.get("APIFY_TOKEN", ""))
DATASET_ID  = "XLbyFxagcoq3KhIE9"
MANUAL_SHEET_CSV = "https://docs.google.com/spreadsheets/d/e/2PACX-1vRF3eiU7dmS8SZBWNz1lffxJHkSJ8yGsK8K_HVyIv5s-kei7TNdcjybHo1mitXO7O-uRmtQ_-eNgbp4/pub?output=csv"
MANUAL_SHEET_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vRF3eiU7dmS8SZBWNz1lffxJHkSJ8yGsK8K_HVyIv5s-kei7TNdcjybHo1mitXO7O-uRmtQ_-eNgbp4/pubhtml"
BASE_URL    = f"https://api.apify.com/v2/datasets/{DATASET_ID}/items"
OUTPUT_DIR  = os.path.dirname(os.path.abspath(__file__))
CSV_PATH    = os.path.join(OUTPUT_DIR, "allocator_jobs.csv")
HTML_PATH   = os.path.join(OUTPUT_DIR, "dashboard.html")

FETCH_LIMIT = 1000   # items per page

# ── Layer 1: Removed — org filtering is handled upstream in Apify ──
LAYER1_ORG_KEYWORDS = [""]   # always passes

# ── Layer 2: Role-type keywords (must appear in job title) ──
LAYER2_TITLE_KEYWORDS = [
    # Core investment titles
    "investment",
    "portfolio",
    "analyst",
    "associate",
    "cio",
    "chief investment officer",
    "chief investment",
    "deputy cio",
    "deputy chief investment",
    "co-cio",
    "investment officer",
    "investment manager",
    "investment director",
    "investment principal",
    "investment professional",
    "investment specialist",
    "investment associate",
    "investment consultant",
    "investment advisor",
    "investment strategist",
    "investment researcher",
    "investment analyst",
    "senior investment",
    "managing director investment",
    # Asset classes
    "alternatives",
    "alternative investments",
    "private equity",
    "private credit",
    "private debt",
    "private markets",
    "infrastructure",
    "real assets",
    "real estate",
    "hedge fund",
    "hedge funds",
    "fixed income",
    "equities",
    "equity",
    "multi-asset",
    "multi asset",
    "credit",
    "structured credit",
    "direct lending",
    "venture capital",
    "venture growth",
    "venture",
    "growth equity",
    "buyout",
    "secondaries",
    "co-investment",
    "co investment",
    "natural resources",
    "commodities",
    "timber",
    "farmland",
    "distressed",
    "mezzanine",
    "emerging markets",
    "global macro",
    "long short",
    "absolute return",
    # Research & due diligence
    "due diligence",
    "manager research",
    "manager selection",
    "fund research",
    "fund selection",
    "manager due diligence",
    "investment research",
    "research analyst",
    "research associate",
    "research director",
    "market research",
    "security selection",
    "stock selection",
    "sector research",
    "fundamental research",
    "quantitative research",
    "esg research",
    # Portfolio & allocation
    "asset allocation",
    "capital markets",
    "portfolio management",
    "portfolio construction",
    "portfolio analyst",
    "portfolio associate",
    "portfolio manager",
    "portfolio officer",
    "portfolio strategist",
    "strategic investments",
    "tactical allocation",
    "strategic allocation",
    "liability driven",
    "liability-driven",
    # Risk & performance
    "risk management",
    "risk analyst",
    "risk officer",
    "risk associate",
    "market risk",
    "investment risk",
    "performance analyst",
    "performance attribution",
    "performance measurement",
    "investment performance",
    # Quant & data
    "quant",
    "quantitative",
    "quantitative analyst",
    "quantitative associate",
    "quantitative strategist",
    "quantitative researcher",
    "quantitative portfolio",
    "data science",
    "data analyst",
    "factor investing",
    "systematic",
    "algorithmic",
    "derivatives",
    "options",
    "futures",
    # Senior / leadership titles
    "head of investments",
    "head of investment",
    "director of investments",
    "director of investment",
    "managing director",
    "executive director",
    "vice president investment",
    "vp investment",
    "principal",
    "partner investment",
    "senior portfolio",
    "senior analyst",
    "senior associate",
    "senior investment",
    "senior manager investment",
    "senior director investment",
    # Allocator-specific org terms in titles
    "endowment",
    "pension",
    "foundation investments",
    "sovereign wealth",
    "ocio",
    "outsourced cio",
    "outsourced chief investment",
    "institutional",
    "fund of funds",
    "fund of hedge funds",
    "asset owner",
    "asset management",
    "family office investment",
    "endowment management",
    "treasury investment",
    # Investment ops & support
    "investment operations",
    "investment governance",
    "investment compliance",
    "investment reporting",
    "investment accounting",
    "investment analytics",
    "investment technology",
    "investment solutions",
    "investment consulting",
    "investment strategy",
    # Trading
    "trader",
    "trading",
    "fixed income trader",
    "equity trader",
    "currency trader",
    "fx trader",
    "derivatives trader",
    "portfolio trader",
    # Finance leadership
    "cfo",
    "chief financial officer",
    "vp finance",
    "head of finance",
    "finance director",
    # ESG / impact / sustainable
    "sustainable investment",
    "mission aligned",
    "mission-aligned",
    "impact investment",
    "impact investing",
    "esg",
    "responsible investment",
    # Valuation & operations
    "valuation",
    "private markets valuation",
    "third party governance",
    "third party risk",
    "governance",
    "public investments",
    "public markets",
    "tangible assets",
    "investment operations",
    "head of operations",
    "coo",
    "chief operating officer",
    "treasurer",
    # Broad seniority/function titles (kept intentionally broad)
    "managing director",
    "director",
    "vice president",
    "head of",
    "md",
    "vp",
]

# ── Exclusion keywords — disqualify on title match ──
EXCLUDE_TITLE_KEYWORDS = [
    # Retail / wealth advisory
    "series 7",
    "wealth advisor",
    "wealth adviser",
    "financial advisor",
    "financial adviser",
    "private client",
    "private banker",
    "wealth management",
    # Sales / BD / client-facing
    "account executive",
    "account manager",
    "relationship manager",
    "client service",
    "customer service",
    "sales",
    "business development",
    # Legal / compliance / audit / tax
    "counsel",
    "attorney",
    "legal",
    "compliance",
    "audit",
    "auditor",
    "tax",
    # Admin / facilities
    "office manager",
    "executive assistant",
    "receptionist",
    "paralegal",
    # Benefits / HR / insurance
    "benefits",
    "health solution",
    "human resources",
    "recruiter",
    "talent",
    # Tech / IT
    "information technology",
    "software engineer",
    "software developer",
    "data engineer",
    "devops",
    "technical business analyst",
    "systems analyst",
    # Marketing
    "marketing",
    "communications",
    # Retail / branch
    "retail",
    "branch",
    # Medical / clinical / healthcare
    "clinical",
    "medical",
    "physician",
    "nurse",
    "nursing",
    "patient",
    "healthcare",
    "health care",
    "clinical research",
    "clinical trial",
    "research coordinator",
    "laboratory",
    "lab technician",
    "radiology",
    "pharmacy",
    "pharmacist",
    "surgical",
    "dental",
    "mental health",
    "social worker",
    "therapist",
    "therapy",
    "public health",
    "epidemiolog",
    "biomedical",
    # Visitor / tours / campus / alumni engagement
    "visitor engagement",
    "visitor services",
    "tour guide",
    "campus tour",
    "alumni engagement",
    "program development",
    "program coordinator",
    "event coordinator",
    "event associate",
    "reception",
    # Customer / client relations (non-investment)
    "customer relations",
    "client relations",
    "client advisor",
    "client associate",
    "client engagement",
    "customer experience",
    "relationship coordinator",
    # HR / people / workplace
    "human capital",
    "people operations",
    "workplace experience",
    "talent acquisition",
    "talent management",
    # Tech / engineering (non-investment)
    "full stack",
    "software",
    "engineer",
    "data governance",
    "data strategy",
    "community connect",
    # Student / intern / academic
    "student",
    "coop",
    "co-op",
    "post doctoral",
    "postdoctoral",
    "post doc",
    # Operations management (non-investment)
    "operations unit manager",
    "operations manager",
    "coordinator",
    # Retirement admin (non-investment)
    "pension administration",
    "plan administrator",
    "retirement analyst",
    "retirement system",
]

CSV_COLUMNS = ["title", "company", "location", "date_posted", "url", "description", "job_type", "source"]

# Browser-like headers to avoid 403s on stricter sites
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# ── API helpers ──────────────────────────────────────────────────────────────

def fetch_all_items() -> list[dict]:
    """Fetch every item from the Apify dataset, handling pagination."""
    items = []
    offset = 0
    print(f"Connecting to Apify dataset {DATASET_ID} …")

    while True:
        params = {
            "token": APIFY_TOKEN,
            "limit": FETCH_LIMIT,
            "offset": offset,
        }
        try:
            resp = requests.get(BASE_URL, params=params, timeout=60)
            resp.raise_for_status()
        except requests.exceptions.HTTPError as e:
            print(f"  HTTP error: {e}")
            sys.exit(1)
        except requests.exceptions.RequestException as e:
            print(f"  Request error: {e}")
            sys.exit(1)

        page = resp.json()

        # Apify can return a list directly or wrap in {"items": [...]}
        if isinstance(page, list):
            batch = page
        elif isinstance(page, dict):
            batch = page.get("items", page.get("data", []))
        else:
            batch = []

        if not batch:
            break

        items.extend(batch)
        print(f"  Fetched {len(items)} items so far …")

        if len(batch) < FETCH_LIMIT:
            break
        offset += FETCH_LIMIT

    print(f"Total raw items fetched: {len(items)}")
    return items


# ── Filtering ────────────────────────────────────────────────────────────────

def _text_from_item(item: dict) -> str:
    """Combine all searchable text fields into one lowercase string."""
    fields = [
        item.get("title", ""),
        item.get("positionName", ""),
        item.get("jobTitle", ""),
        # Apify dataset field names
        item.get("organization", ""),
        item.get("company", ""),
        item.get("companyName", ""),
        item.get("employer", ""),
        item.get("description_text", ""),
        item.get("description", ""),
        item.get("jobDescription", ""),
        item.get("summary", ""),
        item.get("ai_core_responsibilities", ""),
        item.get("ai_requirements_summary", ""),
        # taxonomies / keywords lists
        " ".join(item.get("ai_taxonomies_a", []) or []),
        " ".join(item.get("ai_keywords", []) or []),
        item.get("jobType", ""),
        item.get("sector", ""),
        item.get("industry", ""),
        item.get("category", ""),
    ]
    return " ".join(str(f) for f in fields if f).lower()


def _title_from_item(item: dict) -> str:
    """Return the job title as a lowercase string."""
    return (
        item.get("title") or item.get("positionName") or item.get("jobTitle") or ""
    ).lower()


def _org_text_from_item(item: dict) -> str:
    """Return org-focused fields (name + industry/sector/description) as lowercase."""
    fields = [
        item.get("organization", ""),
        item.get("company", ""),
        item.get("companyName", ""),
        item.get("employer", ""),
        item.get("sector", ""),
        item.get("industry", ""),
        item.get("category", ""),
        " ".join(item.get("ai_taxonomies_a", []) or []),
        " ".join(item.get("ai_keywords", []) or []),
        # Include first 800 chars of description for org-type context
        (item.get("description_text") or item.get("description") or "")[:800],
    ]
    return " ".join(str(f) for f in fields if f).lower()


def passes_layer1(item: dict) -> bool:
    """Layer 1 removed — org filtering handled upstream in Apify."""
    return True


def passes_layer2(item: dict) -> bool:
    """Layer 2: title must contain at least one relevant investment keyword."""
    title = _title_from_item(item)
    return any(kw in title for kw in LAYER2_TITLE_KEYWORDS)


def passes_exclusions(item: dict) -> bool:
    """Return False if the title contains any exclusion keyword."""
    title = _title_from_item(item)
    return not any(kw in title for kw in EXCLUDE_TITLE_KEYWORDS)


# US states (full names + abbreviations) and Canadian provinces
US_GEO = [
    "united states", " usa", ", us,", ", us ", "u.s.",
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana",
    "maine", "maryland", "massachusetts", "michigan", "minnesota",
    "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "new york", "north carolina",
    "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania",
    "rhode island", "south carolina", "south dakota", "tennessee", "texas",
    "utah", "vermont", "virginia", "washington", "west virginia",
    "wisconsin", "wyoming", "washington, d.c", "washington dc",
    # Major cities
    "new york", "boston", "chicago", "los angeles", "san francisco",
    "seattle", "denver", "dallas", "houston", "miami", "atlanta",
    "philadelphia", "minneapolis", "charlotte", "baltimore", "austin",
    "portland", "nashville", "pittsburgh", "st. louis", "detroit",
    "salt lake city", "hartford", "stamford", "greenwich",
]
CANADA_GEO = [
    "canada", "ontario", "quebec", "british columbia", "alberta",
    "manitoba", "saskatchewan", "nova scotia", "new brunswick",
    "toronto", "montreal", "vancouver", "calgary", "edmonton", "ottawa",
]
NORTH_AMERICA_GEO = US_GEO + CANADA_GEO


def passes_geo(item: dict) -> bool:
    """Keep only jobs located in the United States or Canada."""
    loc_derived = item.get("locations_derived") or []
    location = (
        loc_derived[0] if loc_derived
        else item.get("location") or item.get("jobLocation") or item.get("city") or ""
    ).lower()

    # Also check locations_raw for edge cases
    loc_raw = " ".join(
        str(l) for l in (item.get("locations_raw") or [])
    ).lower()

    combined = location + " " + loc_raw

    # If location is empty/remote with no geography, include by default
    if not combined.strip():
        return True

    return any(kw in combined for kw in NORTH_AMERICA_GEO)


def passes_geo_dict(j: dict) -> bool:
    """Geo filter for already-normalised job dicts (supplemental sources)."""
    location = j.get("location", "").lower()
    if not location:
        return True
    return any(kw in location for kw in NORTH_AMERICA_GEO)


def filter_jobs(items: list[dict]) -> list[dict]:
    total = len(items)
    after_l1, after_l2, after_ex, after_geo = [], [], [], []

    for item in items:
        if not passes_layer1(item):
            continue
        after_l1.append(item)
        if not passes_layer2(item):
            continue
        after_l2.append(item)
        if not passes_exclusions(item):
            continue
        after_ex.append(item)
        if not passes_geo(item):
            continue
        after_geo.append(item)

    print(f"  Raw items            : {total}")
    print(f"  After Layer 1 (org)  : {len(after_l1)}")
    print(f"  After Layer 2 (title): {len(after_l2)}")
    print(f"  After exclusions     : {len(after_ex)}")
    print(f"  After geo filter     : {len(after_geo)}")
    return after_geo


# ── Supplemental scrapers ────────────────────────────────────────────────────

def _make_job(title, company, location, url, description="", job_type="", date_posted="", source=""):
    """Build a normalised job dict from raw strings."""
    return {
        "title":       title.strip(),
        "company":     company.strip(),
        "location":    location.strip(),
        "date_posted": date_posted.strip(),
        "url":         url.strip(),
        "description": (description.strip())[:500] + ("…" if len(description.strip()) > 500 else ""),
        "job_type":    job_type.strip(),
        "source":      source,
    }


def _get(url, **kwargs):
    """GET with browser headers; return Response or None on failure."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=30, **kwargs)
        r.raise_for_status()
        return r
    except requests.exceptions.RequestException as e:
        print(f"    [WARN] {url}: {e}")
        return None


def scrape_ncpers() -> list[dict]:
    """
    NCPERS pension job board — static HTML blog page.
    Pattern: <h5>Org</h5> followed by <p><a href=...>Title</a></p>
    """
    SOURCE = "NCPERS"
    URL    = "https://www.ncpers.org/blog/online--pension-industry-careers-job-listings-hiring-and-retirement-announcements"
    print(f"\n[{SOURCE}] Fetching …")
    resp = _get(URL)
    if not resp:
        print(f"  [{SOURCE}] Skipped — could not fetch page.")
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    jobs = []

    # Find the main content area
    content = soup.find("div", class_=re.compile(r"entry-content|post-content|field-body", re.I))
    if not content:
        content = soup.find("main") or soup.body

    # Walk h5 tags — each marks an organisation block
    for h5 in content.find_all("h5"):
        org = h5.get_text(strip=True)
        if not org:
            continue
        # Collect <p> siblings until next h5
        sibling = h5.find_next_sibling()
        while sibling and sibling.name != "h5":
            if sibling.name == "p":
                link = sibling.find("a")
                if link and link.get("href"):
                    href = link["href"]
                    # Skip "Follow this link" / apply links — they're secondary
                    if "follow this link" not in link.get_text(strip=True).lower():
                        title = link.get_text(strip=True)
                        if title:
                            jobs.append(_make_job(
                                title=title,
                                company=org,
                                location="",
                                url=href,
                                source=SOURCE,
                            ))
            sibling = sibling.find_next_sibling()

    print(f"  [{SOURCE}] Found {len(jobs)} raw listings.")
    return jobs


def scrape_allocatorjobs() -> list[dict]:
    """
    AllocatorJobs.com — WordPress / Salient theme.
    Job cards: .nectar-post-grid-item  h3.post-heading > a (title + URL)
    Paginates via ?paged=N. CSS gate hides jobs for logged-out users but
    the HTML is server-rendered; we collect what's visible.
    """
    SOURCE   = "AllocatorJobs"
    BASE     = "https://allocatorjobs.com/"
    print(f"\n[{SOURCE}] Fetching …")
    jobs = []
    page = 1

    while True:
        url  = BASE if page == 1 else f"{BASE}page/{page}/"
        resp = _get(url)
        if not resp:
            break

        soup  = BeautifulSoup(resp.text, "lxml")
        cards = soup.select(".nectar-post-grid-item, article.type-post")

        if not cards:
            break

        for card in cards:
            # Title + URL
            title_tag = card.select_one("h3.post-heading a, h2.post-heading a, .entry-title a")
            if not title_tag:
                continue
            title    = title_tag.get_text(strip=True)
            job_url  = title_tag.get("href", "")

            # Company from image alt or card meta
            img      = card.select_one("img")
            company  = img.get("alt", "").strip() if img else ""
            if not company:
                meta = card.select_one(".meta-category a, .cat-links a")
                company = meta.get_text(strip=True) if meta else ""

            # Snippet / description
            snippet  = card.select_one(".meta-excerpt, .entry-summary, p")
            desc     = snippet.get_text(strip=True) if snippet else ""

            if title:
                jobs.append(_make_job(
                    title=title,
                    company=company,
                    location="",
                    url=job_url,
                    description=desc,
                    source=SOURCE,
                ))

        # Check for next page link
        next_link = soup.select_one("a.next.page-numbers, .nav-previous a, a[rel='next']")
        if not next_link:
            break
        page += 1
        if page > 20:   # safety cap
            break

    print(f"  [{SOURCE}] Found {len(jobs)} raw listings.")
    return jobs


def _scrape_ymcareers(site_url: str, source_name: str, search_terms: list[str]) -> list[dict]:
    """
    Generic scraper for YourMembership/YM Careers boards (careers.cfainstitute.org, jobs.cof.org).
    Tries searching for each term and parses the listing HTML.
    """
    print(f"\n[{source_name}] Fetching …")
    jobs     = []
    seen     = set()

    for term in search_terms:
        page = 1
        while True:
            params = {"keywords": term, "page": page}
            resp   = _get(site_url, params=params)
            if not resp:
                break

            soup     = BeautifulSoup(resp.text, "lxml")
            # YM Careers uses table rows or article/li cards depending on theme
            listings = (
                soup.select("tr.data-row, li.job, article.job-listing, "
                            ".job-listing, .jb-job-list-row, "
                            "h2.job-title, .views-row")
            )

            if not listings:
                # Fallback: look for any <a> with /job/ or /jobs/ in href
                listings = [
                    a for a in soup.select("a[href]")
                    if re.search(r"/job(s)?/\d|/job(s)?/[a-z]", a.get("href", ""), re.I)
                ]

            if not listings:
                break

            for item in listings:
                if hasattr(item, "select_one"):
                    title_tag = item.select_one("a, h2, h3, .job-title, td.views-field-title")
                else:
                    title_tag = item   # item is already an <a>

                if not title_tag:
                    continue

                title   = title_tag.get_text(strip=True)
                href    = title_tag.get("href", "") if title_tag.name == "a" else ""
                if not href and hasattr(title_tag, "find"):
                    a = title_tag.find("a")
                    href = a.get("href", "") if a else ""

                # Make absolute URL
                if href and not href.startswith("http"):
                    from urllib.parse import urljoin
                    href = urljoin(site_url, href)

                # Location
                loc_tag  = item.select_one(".job-location, .location, td.views-field-field-location") if hasattr(item, "select_one") else None
                location = loc_tag.get_text(strip=True) if loc_tag else ""

                # Company / employer
                org_tag  = item.select_one(".employer, .company, td.views-field-field-company") if hasattr(item, "select_one") else None
                company  = org_tag.get_text(strip=True) if org_tag else ""

                uid = (title.lower(), href)
                if title and uid not in seen:
                    seen.add(uid)
                    jobs.append(_make_job(
                        title=title,
                        company=company,
                        location=location,
                        url=href,
                        source=source_name,
                    ))

            # Next page
            next_link = soup.select_one("a.next, a[rel='next'], .pager-next a")
            if not next_link:
                break
            page += 1
            if page > 10:
                break

    print(f"  [{source_name}] Found {len(jobs)} raw listings.")
    return jobs


def scrape_cfa_institute() -> list[dict]:
    return _scrape_ymcareers(
        site_url="https://careers.cfainstitute.org/jobs/",
        source_name="CFA Institute",
        search_terms=["investment", "portfolio", "analyst", "private equity", "alternatives"],
    )


def scrape_cof() -> list[dict]:
    return _scrape_ymcareers(
        site_url="https://jobs.cof.org/jobs/",
        source_name="Council on Foundations",
        search_terms=["investment", "portfolio", "endowment", "analyst", "chief investment officer"],
    )


def scrape_manual_sheet() -> list[dict]:
    """
    Fetch manually added jobs from the published Google Sheet CSV.
    Columns: Title, Company, Location, URL, Date_Posted
    All manual jobs bypass the Layer 2 / exclusion filters — they are
    assumed to be pre-vetted by the person who added them.
    """
    SOURCE = "Manual"
    print(f"\n[{SOURCE}] Fetching Google Sheet …")
    resp = _get(MANUAL_SHEET_CSV)
    if not resp:
        print(f"  [{SOURCE}] Skipped — could not fetch sheet.")
        return []

    jobs = []
    reader = csv.DictReader(resp.text.splitlines())
    for row in reader:
        title      = (row.get("Job Title") or row.get("Title") or row.get("title") or "").strip()
        company    = (row.get("Company Name") or row.get("Company") or row.get("company") or "").strip()
        location   = (row.get("Location") or row.get("location") or "").strip()
        url        = (row.get("URL") or row.get("url") or "").strip()
        date_posted = (row.get("Date_Posted") or row.get("Date posted") or row.get("date_posted") or "").strip()

        if not title and not url:
            continue  # skip empty rows

        jobs.append(_make_job(
            title=title,
            company=company,
            location=location,
            url=url,
            date_posted=date_posted,
            source=SOURCE,
        ))

    print(f"  [{SOURCE}] Found {len(jobs)} manually added jobs.")
    return jobs


def scrape_ultipro(board_url: str, board_id: str, company: str, location: str) -> list[dict]:
    """
    Generic scraper for UltiPro/UKG job boards using the LoadSearchResults API.
    board_url: base URL e.g. https://recruiting2.ultipro.com/UNI1086TAMIM/JobBoard/36bde31a-...
    board_id:  the GUID in the URL path
    """
    SOURCE   = company
    api_url  = f"{board_url}/JobBoardView/LoadSearchResults"
    print(f"\n[{SOURCE}] Fetching via UltiPro API …")

    order_by = [{"Value": "postedDateDesc", "PropertyName": "PostedDate", "Ascending": False}]
    payload = {
        "opportunitySearch": {
            "QueryString": "",
            "Filters": [],
            "Top": 100,
            "Skip": 0,
            "OrderBy": order_by,
        }
    }

    try:
        post_headers = {**HEADERS, "Content-Type": "application/json", "Referer": board_url}
        resp = requests.post(api_url, json=payload, headers=post_headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  [{SOURCE}] Failed: {{e}}")
        return []

    opportunities = data.get("opportunities", [])
    jobs = []
    for opp in opportunities:
        title       = (opp.get("Title") or opp.get("title") or "").strip()
        opp_id      = opp.get("Id") or opp.get("id") or ""
        job_url     = f"{{board_url}}/OpportunityDetail?opportunityId={{opp_id}}" if opp_id else board_url
        date_raw    = opp.get("PostedDate") or opp.get("postedDate") or ""
        if date_raw and "T" in date_raw:
            date_raw = date_raw.split("T")[0]
        loc = opp.get("Location") or opp.get("location") or location

        if title:
            jobs.append(_make_job(
                title=title,
                company=company,
                location=loc,
                url=job_url,
                date_posted=date_raw,
                source=SOURCE,
            ))

    print(f"  [{SOURCE}] Found {{len(jobs)}} jobs.")
    return jobs


def scrape_utimco() -> list[dict]:
    return scrape_ultipro(
        board_url="https://recruiting2.ultipro.com/UNI1086TAMIM/JobBoard/36bde31a-2829-41f6-8302-00354faf172a",
        board_id="36bde31a-2829-41f6-8302-00354faf172a",
        company="UTIMCO",
        location="Austin, TX",
    )


def scrape_all_supplemental() -> list[dict]:
    """Run all supplemental scrapers and return merged, deduped list."""
    all_jobs = []
    all_jobs.extend(scrape_ncpers())
    all_jobs.extend(scrape_allocatorjobs())
    all_jobs.extend(scrape_cfa_institute())
    all_jobs.extend(scrape_cof())
    all_jobs.extend(scrape_utimco())
    all_jobs.extend(scrape_manual_sheet())
    return all_jobs


# ── Normalisation ────────────────────────────────────────────────────────────

def normalise(item: dict) -> dict:
    """Map various possible field names to our canonical CSV columns."""
    title = (
        item.get("title")
        or item.get("positionName")
        or item.get("jobTitle")
        or "N/A"
    )
    company = (
        item.get("organization")          # primary Apify field
        or item.get("company")
        or item.get("companyName")
        or item.get("employer")
        or "N/A"
    )

    # locations_derived is a list like ["Lake Forest, Illinois, United States"]
    loc_derived = item.get("locations_derived") or []
    location = (
        loc_derived[0] if loc_derived
        else item.get("location")
        or item.get("jobLocation")
        or item.get("city")
        or "N/A"
    )

    date_posted = (
        item.get("date_posted")           # primary Apify field
        or item.get("date")
        or item.get("datePosted")
        or item.get("publishedAt")
        or item.get("postedAt")
        or item.get("createdAt")
        or ""
    )
    # Trim to date only (strip time component if present)
    if date_posted and "T" in date_posted:
        date_posted = date_posted.split("T")[0]

    url = (
        item.get("url")
        or item.get("jobUrl")
        or item.get("applyUrl")
        or item.get("link")
        or ""
    )

    description_raw = (
        item.get("description_text")      # primary Apify field (full text)
        or item.get("description")
        or item.get("jobDescription")
        or item.get("summary")
        or item.get("ai_core_responsibilities")
        or ""
    )
    description = description_raw[:500] + ("…" if len(description_raw) > 500 else "")

    # employment_type is a list in Apify data
    raw_et = item.get("employment_type") or item.get("ai_employment_type") or []
    if isinstance(raw_et, list):
        job_type = ", ".join(raw_et)
    else:
        job_type = str(raw_et)
    job_type = job_type or item.get("jobType") or item.get("type") or ""

    return {
        "title":       title,
        "company":     company,
        "location":    location,
        "date_posted": date_posted,
        "url":         url,
        "description": description,
        "job_type":    job_type,
        "source":      item.get("_source", "Apify"),
    }


# ── CSV output ───────────────────────────────────────────────────────────────

def save_csv(jobs: list[dict]) -> None:
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(jobs)
    print(f"CSV saved  → {CSV_PATH}")


# ── HTML dashboard ───────────────────────────────────────────────────────────

def _stats(jobs: list[dict]) -> dict:
    companies = {}
    dates = []
    for j in jobs:
        c = j["company"]
        companies[c] = companies.get(c, 0) + 1
        if j["date_posted"]:
            dates.append(j["date_posted"])

    top_companies = sorted(companies.items(), key=lambda x: x[1], reverse=True)[:8]
    dates_sorted  = sorted(dates)
    date_min = dates_sorted[0][:10]  if dates_sorted else "N/A"
    date_max = dates_sorted[-1][:10] if dates_sorted else "N/A"
    return {
        "total":         len(jobs),
        "top_companies": top_companies,
        "date_min":      date_min,
        "date_max":      date_max,
    }


def _rows_html(jobs: list[dict]) -> str:
    rows = []
    now = datetime.now(timezone.utc).date()
    for j in jobs:
        # Recency badge
        raw_date = j["date_posted"][:10] if j["date_posted"] else ""
        age_class = "old"
        if raw_date:
            try:
                posted = datetime.strptime(raw_date, "%Y-%m-%d").date()
                delta  = (now - posted).days
                if delta <= 7:
                    age_class = "fresh"
                elif delta <= 30:
                    age_class = "recent"
            except ValueError:
                pass

        url = escape(j["url"])
        title_cell = (
            f'<a href="{url}" target="_blank" rel="noopener">{escape(j["title"])}</a>'
            if url else escape(j["title"])
        )
        desc = escape(j["description"])
        source = escape(j.get("source", ""))
        rows.append(
            f'<tr class="age-{age_class}">'
            f'<td>{title_cell}</td>'
            f'<td>{escape(j["company"])}</td>'
            f'<td>{escape(j["location"])}</td>'
            f'<td data-sort="{escape(raw_date)}">{escape(raw_date)}</td>'
            f'<td>{escape(j["job_type"])}</td>'
            f'<td><span class="source-tag{" manual" if j.get("source") == "Manual" else ""}">{source}</span></td>'
            f'</tr>'
        )
    return "\n".join(rows)


def _top_companies_html(top: list[tuple]) -> str:
    items = "".join(
        f'<div class="company-chip"><span class="chip-name">{escape(c)}</span>'
        f'<span class="chip-count">{n}</span></div>'
        for c, n in top
    )
    return items


def generate_html(jobs: list[dict]) -> None:
    stats    = _stats(jobs)
    rows     = _rows_html(jobs)
    chips    = _top_companies_html(stats["top_companies"])
    updated  = datetime.now().strftime("%B %d, %Y at %I:%M %p")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Institutional Allocator Jobs — FRAM Partners</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Source+Serif+4:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
  /* ── FRAM Partners design tokens ── */
  :root {{
    --navy:      #1b2232;
    --burgundy:  #4f1722;
    --cream:     #f9f5f0;
    --white:     #ffffff;
    --border:    #e3e5e8;
    --muted:     #676f7e;
    --font-display: "Source Serif 4", Georgia, serif;
    --font-body:    "Inter", system-ui, sans-serif;
  }}

  /* ── Reset & base ── */
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: var(--font-body); background: var(--cream);
         color: var(--navy); min-height: 100vh; }}
  a {{ color: var(--burgundy); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}

  /* ── Header ── */
  header {{
    background: var(--navy);
    color: var(--white);
    padding: 2.5rem 2rem 2rem;
    border-bottom: 3px solid var(--burgundy);
  }}
  header h1 {{
    font-family: var(--font-display);
    font-size: 2rem; font-weight: 400; letter-spacing: -.5px;
  }}
  header .eyebrow {{
    font-family: var(--font-body);
    font-size: .7rem; font-weight: 500; letter-spacing: .15em;
    text-transform: uppercase; color: var(--burgundy);
    margin-bottom: .6rem;
  }}
  header p {{ margin-top: .4rem; color: rgba(255,255,255,.5); font-size: .85rem;
              font-weight: 300; }}
  .header-inner {{ max-width: 1400px; margin: 0 auto; }}

  /* ── Main container ── */
  .container {{ max-width: 1400px; margin: 0 auto; padding: 2rem 2rem 4rem; }}

  /* ── Stat cards ── */
  .stats-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 1px; margin-bottom: 2rem;
    border: 1px solid var(--border); background: var(--border);
  }}
  .stat-card {{
    background: var(--white);
    padding: 1.5rem;
    border-left: 3px solid transparent;
  }}
  .stat-card:first-child {{ border-left-color: var(--burgundy); }}
  .stat-card .label {{
    font-size: .65rem; text-transform: uppercase;
    letter-spacing: .12em; color: var(--muted);
    font-weight: 500; margin-bottom: .5rem;
  }}
  .stat-card .value {{
    font-family: var(--font-display);
    font-size: 1.9rem; font-weight: 400; color: var(--navy);
  }}

  /* ── Companies section ── */
  .section {{
    background: var(--white); border: 1px solid var(--border);
    padding: 1.5rem; margin-bottom: 1.5rem;
  }}
  .section h2 {{
    font-family: var(--font-display);
    font-size: .95rem; font-weight: 400; color: var(--navy);
    margin-bottom: 1rem; padding-bottom: .6rem;
    border-bottom: 1px solid var(--border);
    letter-spacing: .01em;
  }}
  .companies-wrap {{ display: flex; flex-wrap: wrap; gap: .5rem; }}
  .company-chip {{
    display: flex; align-items: center; gap: .4rem;
    background: var(--cream); border: 1px solid var(--border);
    padding: .3rem .75rem; font-size: .8rem;
  }}
  .chip-name {{ color: var(--navy); font-weight: 500; }}
  .chip-count {{
    background: var(--burgundy); color: var(--white);
    padding: .1rem .45rem; font-size: .68rem; font-weight: 600;
  }}

  /* ── Table section ── */
  .table-section {{ background: var(--white); border: 1px solid var(--border); overflow: hidden; }}
  .table-toolbar {{
    display: flex; align-items: center; justify-content: space-between;
    flex-wrap: wrap; gap: 1rem;
    padding: 1.25rem 1.5rem; border-bottom: 1px solid var(--border);
    background: var(--white);
  }}
  .table-toolbar h2 {{
    font-family: var(--font-display);
    font-size: .95rem; font-weight: 400; color: var(--navy);
  }}
  #searchBox {{
    padding: .5rem 1rem; border: 1px solid var(--border);
    border-radius: 0; font-family: var(--font-body);
    font-size: .8rem; outline: none; width: 280px; max-width: 100%;
    transition: border-color .2s; background: var(--white); color: var(--navy);
  }}
  #searchBox:focus {{ border-color: var(--navy); box-shadow: none; }}

  .table-wrap {{ overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; font-size: .82rem; }}
  th {{
    background: var(--navy); color: rgba(255,255,255,.8);
    font-family: var(--font-body); font-weight: 500;
    font-size: .65rem; text-transform: uppercase; letter-spacing: .1em;
    text-align: left; padding: .75rem 1rem;
    white-space: nowrap; cursor: pointer; user-select: none;
  }}
  th:hover {{ background: #24304a; }}
  th .sort-icon {{ margin-left: .3rem; opacity: .4; }}
  th.asc .sort-icon::after  {{ content: " ▲"; }}
  th.desc .sort-icon::after {{ content: " ▼"; }}
  td {{ padding: .75rem 1rem; border-bottom: 1px solid var(--border); vertical-align: top; }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: var(--cream); }}
  .desc-cell {{ color: var(--muted); font-size: .78rem; max-width: 320px; }}

  /* ── Recency colour coding ── */
  tr.age-fresh td:nth-child(4) {{ color: #2d6a4f; font-weight: 600; }}
  tr.age-recent td:nth-child(4) {{ color: #92400e; }}
  tr.age-old td:nth-child(4) {{ color: var(--muted); }}

  /* left border accent per row */
  tr.age-fresh  {{ border-left: 3px solid #2d6a4f; }}
  tr.age-recent {{ border-left: 3px solid #d97706; }}
  tr.age-old    {{ border-left: 3px solid var(--border); }}

  /* ── Footer ── */
  footer {{
    text-align: center; color: var(--muted); font-size: .75rem;
    margin-top: 3rem; padding-top: 1.5rem; border-top: 1px solid var(--border);
    font-weight: 300; letter-spacing: .03em;
  }}

  /* ── Responsive ── */
  @media (max-width: 640px) {{
    header {{ padding: 1.5rem 1rem 1.25rem; }}
    .container {{ padding: 1rem 1rem 2rem; }}
    #searchBox {{ width: 100%; }}
  }}

  /* ── No results row ── */
  #noResults {{ display: none; text-align: center; padding: 2rem; color: var(--muted); }}

  /* ── Source tag ── */
  .source-tag {{
    display: inline-block; padding: .15rem .5rem;
    font-size: .68rem; font-weight: 500;
    background: var(--cream); border: 1px solid var(--border);
    color: var(--navy); white-space: nowrap; letter-spacing: .03em;
  }}
  .source-tag.manual {{
    background: var(--burgundy); border-color: var(--burgundy);
    color: #fff;
  }}

  /* ── Source filter dropdown in toolbar ── */
  #sourceFilter {{
    padding: .45rem .8rem; border: 1px solid var(--border); border-radius: 0;
    font-family: var(--font-body); font-size: .8rem; outline: none;
    color: var(--navy); background: var(--white); cursor: pointer;
  }}
  #sourceFilter:focus {{ border-color: var(--navy); }}

  /* ── Region dropdown inside Location th ── */
  .loc-th {{ position: relative; }}
  .loc-th select {{
    margin-left: .4rem; padding: .15rem .3rem;
    border: 1px solid rgba(255,255,255,.3); border-radius: 0;
    font-size: .7rem; font-weight: 500; font-family: var(--font-body);
    color: var(--white); background: var(--navy);
    cursor: pointer; vertical-align: middle;
  }}
  .loc-th select:focus {{ outline: none; border-color: var(--white); }}
  .loc-th select option {{ background: var(--navy); color: var(--white); }}
</style>
</head>
<body>

<header>
  <div class="header-inner">
    <img src="https://framcompany.com/assets/fram-logo-BYQ0yDsY.png" alt="FRAM Partners" style="height:36px;margin-bottom:1.25rem;display:block;" />
    <h1>Institutional Allocator Jobs</h1>
    <p>Endowments, pensions, family offices, sovereign wealth &amp; private markets — US &amp; Canada</p>
  </div>
</header>

<div class="container">

  <!-- Stats cards -->
  <div class="stats-grid">
    <div class="stat-card">
      <div class="label">Total Roles</div>
      <div class="value">{stats["total"]}</div>
    </div>
    <div class="stat-card">
      <div class="label">Earliest Posted</div>
      <div class="value" style="font-size:1.2rem;padding-top:.2rem;">{stats["date_min"]}</div>
    </div>
    <div class="stat-card">
      <div class="label">Latest Posted</div>
      <div class="value" style="font-size:1.2rem;padding-top:.2rem;">{stats["date_max"]}</div>
    </div>
    <div class="stat-card">
      <div class="label">Last Updated</div>
      <div class="value" style="font-size:.85rem;padding-top:.4rem;line-height:1.5;font-family:var(--font-body);font-weight:400;">{updated}</div>
    </div>
  </div>

  <!-- Top companies -->
  <div class="section">
    <h2>Top Hiring Organizations</h2>
    <div class="companies-wrap">
      {chips if chips else '<span style="color:#94a3b8;font-size:.875rem;">No data</span>'}
    </div>
  </div>

  <!-- Jobs table -->
  <div class="table-section">
    <div class="table-toolbar">
      <h2>All Roles ({stats["total"]})</h2>
      <div style="display:flex;gap:.6rem;flex-wrap:wrap;align-items:center;">
        <input id="searchBox" type="search" placeholder="Search title, company, location …" />
        <select id="sourceFilter">
          <option value="all">All sources</option>
          <option value="Apify">Apify</option>
          <option value="NCPERS">NCPERS</option>
          <option value="AllocatorJobs">AllocatorJobs</option>
          <option value="CFA Institute">CFA Institute</option>
          <option value="Council on Foundations">Council on Foundations</option>
          <option value="UTIMCO">UTIMCO</option>
          <option value="Manual">Manually Added</option>
        </select>
      </div>
    </div>
    <div class="table-wrap">
      <table id="jobsTable">
        <thead>
          <tr>
            <th data-col="0">Title <span class="sort-icon"></span></th>
            <th data-col="1">Company <span class="sort-icon"></span></th>
            <th data-col="2" class="loc-th">Location <span class="sort-icon"></span><select id="regionFilter" onclick="event.stopPropagation()"><option value="all">All regions</option><option value="us">United States</option><option value="canada">Canada</option></select></th>
            <th data-col="3">Date Posted <span class="sort-icon"></span></th>
            <th data-col="4">Type <span class="sort-icon"></span></th>
            <th data-col="5">Source <span class="sort-icon"></span></th>
          </tr>
        </thead>
        <tbody id="tableBody">
          {rows if rows else '<tr><td colspan="7" style="text-align:center;padding:2rem;color:#94a3b8;">No allocator roles found in dataset.</td></tr>'}
        </tbody>
      </table>
      <div id="noResults">No results match your search.</div>
    </div>
  </div>

  <footer>
    <p style="margin-top:1.5rem;">Generated by allocator-jobs scraper &bull; {updated}</p>
    <p style="margin-top:.4rem;">
      <span style="color:#2d6a4f;">&#9632;</span> Posted &le; 7 days &nbsp;
      <span style="color:#d97706;">&#9632;</span> Posted 8–30 days &nbsp;
      <span style="color:#e3e5e8;">&#9632;</span> Older
    </p>
  </footer>

</div>

<script>
// ── Region classification ──
const US_KW   = ["united states","new york","new jersey","connecticut","california","illinois",
                  "texas","massachusetts","florida","georgia","pennsylvania","ohio","virginia",
                  "washington","boston","chicago","los angeles","san francisco","miami","dallas",
                  "houston","seattle","denver","charlotte","atlanta"," ny,"," nj,"," ct,",
                  " ca,"," il,"," tx,"," ma,"," fl,"," ga,"," pa,"," oh,"," va,"," wa,"];
const CANADA_KW = ["canada","ontario","british columbia","alberta","quebec","manitoba",
                    "saskatchewan","nova scotia","new brunswick","newfoundland","prince edward island",
                    "toronto","vancouver","montreal","calgary","edmonton","ottawa","winnipeg",
                    "hamilton","london, on","kitchener","victoria, bc","halifax"];

function classifyRegion(loc) {{
  const l = loc.toLowerCase();
  if (CANADA_KW.some(k => l.includes(k))) return "canada";
  if (US_KW.some(k => l.includes(k)))     return "us";
  return "other";
}}

// ── Search + Region + Source filter ──
const searchBox    = document.getElementById("searchBox");
const regionFilter = document.getElementById("regionFilter");
const sourceFilter = document.getElementById("sourceFilter");
const tableBody    = document.getElementById("tableBody");
const noResults    = document.getElementById("noResults");

function filterTable() {{
  const q      = searchBox.value.toLowerCase().trim();
  const region = regionFilter.value;
  const src    = sourceFilter.value;
  let visible  = 0;
  Array.from(tableBody.rows).forEach(row => {{
    const text    = row.textContent.toLowerCase();
    const loc     = row.cells[2] ? row.cells[2].textContent : "";
    const rowSrc  = row.cells[5] ? row.cells[5].textContent.trim() : "";
    const matchQ  = !q || text.includes(q);
    const matchR  = region === "all" || classifyRegion(loc) === region;
    const matchS  = src === "all" || rowSrc === src;
    const show    = matchQ && matchR && matchS;
    row.style.display = show ? "" : "none";
    if (show) visible++;
  }});
  noResults.style.display = visible === 0 ? "block" : "none";
}}

searchBox.addEventListener("input", filterTable);
regionFilter.addEventListener("change", filterTable);
sourceFilter.addEventListener("change", filterTable);

// ── Column sort ──
let sortCol = -1, sortAsc = true;

document.querySelectorAll("th[data-col]").forEach(th => {{
  th.addEventListener("click", () => {{
    const col = parseInt(th.dataset.col);
    if (sortCol === col) {{
      sortAsc = !sortAsc;
    }} else {{
      sortCol = col; sortAsc = true;
    }}
    document.querySelectorAll("th").forEach(t => t.classList.remove("asc", "desc"));
    th.classList.add(sortAsc ? "asc" : "desc");
    sortTableByCol(col, sortAsc);
  }});
}});

function sortTableByCol(col, asc) {{
  const rows = Array.from(tableBody.rows);
  rows.sort((a, b) => {{
    const ta = (a.cells[col].dataset.sort || a.cells[col].textContent).trim().toLowerCase();
    const tb = (b.cells[col].dataset.sort || b.cells[col].textContent).trim().toLowerCase();
    return asc ? ta.localeCompare(tb) : tb.localeCompare(ta);
  }});
  rows.forEach(r => tableBody.appendChild(r));
}}
</script>

</body>
</html>
"""

    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Dashboard saved → {HTML_PATH}")


# ── Main ─────────────────────────────────────────────────────────────────────

def _dedup(jobs: list[dict]) -> list[dict]:
    """Remove duplicate jobs by URL (case-insensitive), then by title+company."""
    seen_urls    = set()
    seen_titles  = set()
    unique       = []
    for j in jobs:
        url_key   = j["url"].lower().rstrip("/") if j["url"] else ""
        title_key = (j["title"].lower().strip(), j["company"].lower().strip())
        if url_key and url_key in seen_urls:
            continue
        if title_key in seen_titles:
            continue
        if url_key:
            seen_urls.add(url_key)
        seen_titles.add(title_key)
        unique.append(j)
    return unique


def main():
    print("=" * 60)
    print("  Institutional Allocator Job Scraper")
    print("=" * 60)

    # ── 1. Apify dataset ──────────────────────────────────────────
    print("\n[Apify] Fetching dataset …")
    raw_items = fetch_all_items()

    if not raw_items:
        print("No items returned from Apify dataset. Check token and dataset ID.")
        sys.exit(1)

    filtered_apify = filter_jobs(raw_items)
    apify_jobs     = [normalise(j) for j in filtered_apify]
    print(f"  [Apify] {len(apify_jobs)} jobs after filtering.")

    # ── 2. Supplemental sources ───────────────────────────────────
    supp_raw  = scrape_all_supplemental()

    # Apply the same two-layer filter + exclusions to supplemental jobs
    # (they come in as already-normalised dicts, so we filter on the dict fields)
    def _supp_passes(j: dict) -> bool:
        # Manual sheet entries are pre-vetted — always include them
        if j.get("source") == "Manual":
            return True
        title = j["title"].lower()
        if not any(kw in title for kw in LAYER2_TITLE_KEYWORDS):
            return False
        if any(kw in title for kw in EXCLUDE_TITLE_KEYWORDS):
            return False
        if not passes_geo_dict(j):
            return False
        return True

    supp_jobs = [j for j in supp_raw if _supp_passes(j)]
    print(f"\n  Supplemental raw: {len(supp_raw)}  →  after filter: {len(supp_jobs)}")

    # ── 3. Merge + dedup ──────────────────────────────────────────
    # Separate manual jobs so they are never dropped by dedup
    manual_jobs = [j for j in supp_jobs if j.get("source") == "Manual"]
    non_manual  = [j for j in supp_jobs if j.get("source") != "Manual"]

    deduped = _dedup(apify_jobs + non_manual)
    # Sort non-manual jobs by date descending (blank dates go last)
    deduped.sort(key=lambda j: j["date_posted"] or "0000", reverse=True)
    # Manual jobs always pinned to top
    all_jobs = manual_jobs + deduped

    print(f"\n  Total unique jobs: {len(all_jobs)}")

    # ── 4. Output ─────────────────────────────────────────────────
    save_csv(all_jobs)
    generate_html(all_jobs)

    # Summary by source
    from collections import Counter
    sources = Counter(j["source"] for j in all_jobs)
    print("\n  Jobs by source:")
    for src, n in sorted(sources.items()):
        print(f"    {src:30s} {n}")

    print("\nDone!")
    print(f"  CSV       : {CSV_PATH}")
    print(f"  Dashboard : {HTML_PATH}")
    print("=" * 60)


if __name__ == "__main__":
    main()
