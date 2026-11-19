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
APIFY_TOKEN    = _env_vars.get("APIFY_TOKEN", os.environ.get("APIFY_TOKEN", ""))
GH_TOKEN       = _env_vars.get("GH_TOKEN", os.environ.get("GH_TOKEN", ""))
# Actor Task IDs — stable IDs that always point to the latest run
ACTOR_TASK_IDS = ["jDown13k7veexhpaZ", "iJ7jIqOFzpvnWWtpk", "7VDcxoQjS4RJCp3MQ", "7CkdmGSDpEHQVhgQH",
                  "kmitchell123~career-site-job-listing-api-task-6"]  # pilot (6m backfill; switch to 24h after first ingest)
# Fallback static dataset IDs — cleared once historical data is in the CSV
FALLBACK_DATASET_IDS = []   # old backfill IDs removed (all returned 404)
MANUAL_SHEET_CSV = "https://docs.google.com/spreadsheets/d/e/2PACX-1vRF3eiU7dmS8SZBWNz1lffxJHkSJ8yGsK8K_HVyIv5s-kei7TNdcjybHo1mitXO7O-uRmtQ_-eNgbp4/pub?output=csv"
MANUAL_SHEET_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vRF3eiU7dmS8SZBWNz1lffxJHkSJ8yGsK8K_HVyIv5s-kei7TNdcjybHo1mitXO7O-uRmtQ_-eNgbp4/pubhtml"
APIFY_BASE      = "https://api.apify.com/v2/datasets/{dataset_id}/items"
APIFY_TASK_BASE = "https://api.apify.com/v2/actor-tasks/{task_id}/runs"
JOB_MAX_AGE_DAYS = 60  # drop jobs older than this from the CSV
OUTPUT_DIR        = os.path.dirname(os.path.abspath(__file__))
CSV_PATH          = os.path.join(OUTPUT_DIR, "allocator_jobs.csv")
HTML_PATH         = os.path.join(OUTPUT_DIR, "dashboard.html")
ADMIN_PATH        = os.path.join(OUTPUT_DIR, "admin.html")
HIDDEN_JOBS_PATH  = os.path.join(OUTPUT_DIR, "hidden_jobs.txt")

FETCH_LIMIT = 1000   # items per page

def _load_phrase_list(filename: str) -> list[str]:
    """Read a keyword list from data/<filename>: one phrase per line, '#' starts a comment.
    Stops the run if the file is missing or empty - an empty include list would drop every job."""
    path = pathlib.Path(__file__).parent / "data" / filename
    try:
        phrases = [l.strip().lower() for l in path.read_text(encoding="utf-8").splitlines()
                   if l.strip() and not l.strip().startswith("#")]
    except OSError as e:
        sys.exit(f"STOP: cannot read data/{filename}: {e}")
    if not phrases:
        sys.exit(f"STOP: data/{filename} has no phrases - refusing to run (would filter out every job).")
    return phrases



# ── Role-type keywords (must appear in job title): edit data/title_include.txt ──
LAYER2_TITLE_KEYWORDS = _load_phrase_list("title_include.txt")

# ── Exclusion keywords: edit data/title_exclude.txt (no code change needed) ──
EXCLUDE_TITLE_KEYWORDS = _load_phrase_list("title_exclude.txt")

CSV_COLUMNS = ["title", "company", "location", "date_posted", "url", "description", "job_type", "source", "date_added",
               "job_country", "job_state", "seniority",
               "org_priority", "org_sector", "org_region", "org_hq", "org_size_b", "experience_years", "status"]
EXTRA_COLUMNS = CSV_COLUMNS[9:]

RUN_ERRORS: list[str] = []   # any fetch problem is recorded here; main() exits non-zero if non-empty

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

def _fetch_items(dataset_id: str, extra_params: dict = None) -> list[dict]:
    """Fetch items from an Apify dataset with optional extra params (e.g. date filter)."""
    url = APIFY_BASE.format(dataset_id=dataset_id)
    items = []
    offset = 0
    params_base = {"token": APIFY_TOKEN, "limit": FETCH_LIMIT}
    if extra_params:
        params_base.update(extra_params)

    while True:
        params = {**params_base, "offset": offset}
        try:
            resp = requests.get(url, params=params, timeout=60)
            resp.raise_for_status()
        except requests.exceptions.HTTPError as e:
            print(f"  HTTP error: {e}")
            RUN_ERRORS.append(f"dataset {dataset_id}: HTTP error {e}")
            return items
        except requests.exceptions.RequestException as e:
            print(f"  Request error: {e}")
            RUN_ERRORS.append(f"dataset {dataset_id}: request error {e}")
            return items

        page = resp.json()
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


APIFY_API = "https://api.apify.com/v2"
ACTOR_SLUG = "fantastic-jobs~career-site-job-listing-api"
ONE_OFF_WINDOW_HOURS = 72     # an unscheduled (manual) task is ingested for this long after its last run
STALE_ERROR_HOURS    = 48     # a scheduled task with no successful run in this long counts as a failure


def _api_get(path: str, params: dict | None = None):
    resp = requests.get(f"{APIFY_API}{path}", params={"token": APIFY_TOKEN, **(params or {})}, timeout=30)
    resp.raise_for_status()
    body = resp.json()
    return body.get("data", body) if isinstance(body, dict) else body


def discover_tasks() -> list[dict] | None:
    """All of this account's tasks for the job-listing actor, each flagged if an enabled schedule runs it.
    Returns None if discovery fails (the caller then falls back to ACTOR_TASK_IDS)."""
    try:
        actor_id = _api_get(f"/acts/{ACTOR_SLUG}")["id"]
        tasks = _api_get("/actor-tasks", {"limit": 1000}).get("items", [])
        schedules = _api_get("/schedules", {"limit": 1000}).get("items", [])
        scheduled = set()
        for sch in schedules:
            if sch.get("isEnabled", True):
                for act in sch.get("actions", []) or []:
                    if act.get("type") == "RUN_ACTOR_TASK" and act.get("actorTaskId"):
                        scheduled.add(act["actorTaskId"])
        return [{"id": t["id"], "name": t.get("name", t["id"]), "scheduled": t["id"] in scheduled}
                for t in tasks if t.get("actId") == actor_id]
    except Exception as e:
        print(f"  [WARN] Task discovery failed ({type(e).__name__}: {e}); using the built-in task list.")
        return None


def _get_latest_run(task_id: str) -> tuple[str | None, datetime | None]:
    """Return (dataset ID, finish time) of the most recent successful run of an actor task."""
    url = APIFY_TASK_BASE.format(task_id=task_id)
    try:
        resp = requests.get(url, params={"token": APIFY_TOKEN, "limit": 1, "desc": "true", "status": "SUCCEEDED"}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and "data" in data:
            data = data["data"]
        items = data if isinstance(data, list) else data.get("items", [])
        if items:
            fin = items[0].get("finishedAt") or items[0].get("startedAt")
            when = datetime.fromisoformat(fin.replace("Z", "+00:00")) if fin else None
            return items[0].get("defaultDatasetId"), when
    except Exception as e:
        print(f"  [WARN] Could not fetch latest run for task {task_id}: {type(e).__name__}: {e}")
        RUN_ERRORS.append(f"task {task_id}: could not fetch latest run ({type(e).__name__})")
    return None, None


def _has_any_run(task_id: str) -> bool:
    """True if the task has at least one run of any status (a failed one counts)."""
    try:
        resp = requests.get(APIFY_TASK_BASE.format(task_id=task_id),
                            params={"token": APIFY_TOKEN, "limit": 1, "desc": "true"}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        data = data.get("data", data) if isinstance(data, dict) else data
        items = data if isinstance(data, list) else data.get("items", [])
        return bool(items)
    except Exception:
        return True       # can't tell: be cautious and treat it as a real problem


def fetch_recent_items(now: datetime | None = None) -> list[dict]:
    """Fetch items from the latest run of every relevant task, found automatically:
    scheduled tasks always (and a stale one is flagged as a failure); unscheduled tasks only for a few
    days after their last run, so a manual backfill is ingested once and then drops out."""
    now = now or datetime.now(timezone.utc)
    found = discover_tasks()
    if found is None:
        tasks = [{"id": t, "name": t, "scheduled": True} for t in ACTOR_TASK_IDS]
    else:
        tasks = found
    print(f"  [Tasks] {len(tasks)} task(s) for this actor ({sum(t['scheduled'] for t in tasks)} scheduled).")
    all_items = []

    for t in tasks:
        task_id, label = t["id"], f"{t['name']} ({t['id']})"
        ds_id, when = _get_latest_run(task_id)
        age_h = (now - when).total_seconds() / 3600 if when else None
        if not ds_id:
            if t["scheduled"] and _has_any_run(task_id):
                print(f"  [WARN] Scheduled task {label} has runs but none succeeded.")
                RUN_ERRORS.append(f"task {label}: has runs but none succeeded")
            elif t["scheduled"]:
                print(f"  [Tasks] scheduled task {label} has not run yet (waiting for its first run).")
            else:
                print(f"  [Tasks] skipping {label}: never run successfully.")
            continue
        if not t["scheduled"] and age_h is not None and age_h > ONE_OFF_WINDOW_HOURS:
            print(f"  [Tasks] skipping {label}: unscheduled, last run {age_h:.0f}h ago (already ingested).")
            continue
        if t["scheduled"] and age_h is not None and age_h > STALE_ERROR_HOURS:
            print(f"  [WARN] Scheduled task {label} last succeeded {age_h:.0f}h ago.")
            RUN_ERRORS.append(f"task {label}: last successful run was {age_h:.0f}h ago")
        print(f"Fetching dataset {ds_id} for task {label} …")
        items = _fetch_items(ds_id)
        print(f"  {len(items)} items fetched.")
        all_items.extend(items)

    return all_items


def fetch_one_time_datasets() -> list[dict]:
    """Fetch items from one-time historical datasets, filtered to last JOB_MAX_AGE_DAYS days by date_posted."""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=JOB_MAX_AGE_DAYS)).strftime("%Y-%m-%d")
    all_items = []
    for ds_id in FALLBACK_DATASET_IDS:
        print(f"Fetching historical dataset {ds_id} (excluding jobs before {cutoff}) …")
        items = _fetch_items(ds_id)
        # Filter out jobs older than JOB_MAX_AGE_DAYS; use createdAt as fallback if no date_posted
        filtered = []
        for item in items:
            dp = (item.get("date_posted") or item.get("datePosted") or item.get("date") or "")[:10]
            if dp:
                if dp < cutoff:
                    continue
            else:
                created = (item.get("createdAt") or "")[:10]
                if created and created < cutoff:
                    continue
            filtered.append(item)
        print(f"  {len(filtered)} of {len(items)} items within date range.")
        all_items.extend(filtered)
    return all_items


def load_csv_jobs() -> list[dict]:
    """Load existing jobs from the CSV as the historical base."""
    p = pathlib.Path(CSV_PATH)
    if not p.exists():
        return []
    jobs = []
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            jobs.append({
                "title":       row.get("title", ""),
                "company":     row.get("company", ""),
                "location":    row.get("location", ""),
                "date_posted": row.get("date_posted", ""),
                "url":         row.get("url", ""),
                "description": row.get("description", ""),
                "job_type":    row.get("job_type", ""),
                "source":      row.get("source", "Apify"),
                "date_added":  row.get("date_added", "") or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                **{c: row.get(c, "") for c in EXTRA_COLUMNS},
            })
    print(f"  Loaded {len(jobs)} jobs from existing CSV.")
    return jobs


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


_ORG_DOMAINS = None
_ORG_META = None


def _match_org(table: dict, company: str):
    """Look up a company in a search_term-keyed table: exact match first, then the longest term that
    appears in the company name as whole words ('Careers at KKR' -> 'kkr', 'Berkshire Group, LLC' ->
    'berkshire group'). With locked domains this also catches look-alikes such as 'Weber Blackstone'."""
    c = (company or "").strip().lower()
    if c in table:
        return table[c]
    for term in sorted(table, key=len, reverse=True):
        if _re.search(r"(?<![a-z0-9])" + _re.escape(term) + r"(?![a-z0-9])", c):
            return table[term]
    return None


def _load_org_meta() -> dict:
    """search_term (lowercase) -> row from data/orgs.csv (priority, sector, region, hq, size_b)."""
    global _ORG_META
    if _ORG_META is None:
        _ORG_META = {}
        path = pathlib.Path(__file__).parent / "data" / "orgs.csv"
        try:
            with open(path, newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    t = (r.get("search_term") or r.get("org_name") or "").strip().lower()
                    if t:
                        _ORG_META.setdefault(t, r)      # first row wins when firms share a term (parent listed first)
        except FileNotFoundError:
            pass
    return _ORG_META


def write_run_report(raw: list[dict], kept: list[dict]) -> None:
    """Per-firm coverage: jobs returned by Apify vs jobs kept. Flags firms with zero."""
    from collections import Counter
    def _name(i): return str(i.get("organization") or i.get("company") or "").strip().lower()
    terms = {t: t for t in _load_org_meta()}
    def _term(i):
        n = _name(i)
        return _match_org(terms, n) or n
    valid = [i for i in raw if passes_layer1(i)]
    raw_c, kept_c = Counter(_term(i) for i in valid), Counter(_term(i) for i in kept)
    reject_c = Counter(_term(i) for i in raw if not passes_layer1(i))
    rows = []
    for term, r in _load_org_meta().items():
        rows.append({"firm": r.get("org_name", term), "search_term": r.get("search_term", ""),
                     "priority": r.get("priority", ""), "raw_jobs": raw_c.get(term, 0),
                     "kept_jobs": kept_c.get(term, 0), "lookalikes_rejected": reject_c.get(term, 0)})
    if not rows:
        return
    with open("run_report.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)
    silent = [r["firm"] for r in rows if r["raw_jobs"] == 0]
    print(f"\n  [Coverage] {len(rows) - len(silent)} of {len(rows)} tracked firms returned jobs.")
    if silent:
        print(f"  [Coverage] No jobs from: {', '.join(silent)}")

def _load_org_domains() -> dict:
    """search_term (lowercase) -> locked domain, from data/orgs.csv. Only firms with a locked domain."""
    global _ORG_DOMAINS
    if _ORG_DOMAINS is None:
        _ORG_DOMAINS = {}
        path = pathlib.Path(__file__).parent / "data" / "orgs.csv"
        try:
            with open(path, newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    d = (r.get("domain") or "").strip().lower()
                    t = (r.get("search_term") or r.get("org_name") or "").strip().lower()
                    if d and t:
                        _ORG_DOMAINS[t] = d
        except FileNotFoundError:
            pass
    return _ORG_DOMAINS


def passes_layer1(item: dict) -> bool:
    """Layer 1: domain allowlist. If the job's company name matches a firm in
    data/orgs.csv that has a locked domain, the job's domain must equal it
    (blocks look-alike companies, e.g. wrong 'Citadel'). Other orgs pass."""
    org = str(item.get("organization") or "").strip().lower()
    dom = str(item.get("domain_derived") or "").strip().lower()
    expected = _match_org(_load_org_domains(), org)
    if expected:
        if not dom:                       # some job sources give no domain: accept only an exact firm-name match
            return org in _load_org_domains()
        return dom == expected or dom.endswith("." + expected)
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


# ── Link check: is the job page still there? ────────────────────────────────
def _link_mode() -> str:
    """env LINK_CHECK_MODE, else data/link_check_mode.txt, else 'report'. Values: report | remove | off."""
    m = os.environ.get("LINK_CHECK_MODE", "").strip().lower()
    if not m:
        try:
            m = (pathlib.Path(__file__).parent / "data" / "link_check_mode.txt").read_text().strip().lower()
        except OSError:
            m = ""
    return m if m in ("report", "remove", "off") else "report"


LINK_CHECK_MODE = _link_mode()
LINK_CHECK_MIN_AGE_DAYS = 7      # only check jobs at least this old (fresh ones are almost surely open)
LINK_CHECK_MAX_PER_RUN  = 600    # cap so a run stays quick and polite


LINK_DETAIL: dict = {}      # url -> short reason for an 'unknown' result, for the log


def _check_one(url: str) -> str:
    """Return 'gone', 'ok' or 'unknown'. 'gone' only on clear signals: 404/410, or Greenhouse's error redirect."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=12, allow_redirects=True, stream=True)
        status, final = resp.status_code, resp.url or url
        resp.close()
    except requests.exceptions.RequestException as e:
        LINK_DETAIL[url] = type(e).__name__
        return "unknown"
    if status in (404, 410):
        return "gone"
    if "greenhouse.io" in final and "error=true" in final:
        return "gone"
    if 200 <= status < 300:
        return "ok"
    LINK_DETAIL[url] = str(status)
    return "unknown"          # 403 / 429 / 5xx etc: say nothing rather than guess


def check_links(jobs: list[dict], today=None) -> list[dict]:
    """Check each older non-manual job's page. In 'report' mode nothing is removed; in 'remove' mode
    jobs whose page is clearly gone are dropped. Never raises and never fails the run."""
    if LINK_CHECK_MODE == "off":
        return jobs
    from concurrent.futures import ThreadPoolExecutor
    from datetime import timedelta
    today = today or datetime.now(timezone.utc).date()
    cutoff = (today - timedelta(days=LINK_CHECK_MIN_AGE_DAYS)).strftime("%Y-%m-%d")
    candidates = [j for j in jobs
                  if j.get("source") != "Manual" and j.get("url", "").startswith("http")
                  and ((j.get("date_posted") or j.get("date_added") or "")[:10] <= cutoff)]
    candidates.sort(key=lambda j: (j.get("date_posted") or j.get("date_added") or ""))   # oldest first
    candidates = candidates[:LINK_CHECK_MAX_PER_RUN]
    print(f"\n  [Link check] mode={LINK_CHECK_MODE}; checking {len(candidates)} job link(s) …")
    try:
        with ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(lambda j: _check_one(j["url"]), candidates))
    except Exception as e:                      # pragma: no cover
        print(f"  [Link check] skipped: {type(e).__name__}: {e}")
        return jobs
    from collections import Counter
    tally = Counter(results)
    print(f"  [Link check] ok={tally['ok']} gone={tally['gone']} unknown={tally['unknown']}")
    if tally["unknown"]:
        from urllib.parse import urlparse
        why = Counter((urlparse(j["url"]).netloc.split(".")[-2:][0] if "." in urlparse(j["url"]).netloc else urlparse(j["url"]).netloc,
                       LINK_DETAIL.get(j["url"], "?")) for j, r in zip(candidates, results) if r == "unknown")
        print("  [Link check] unknown by site/reason: " + ", ".join(f"{h}:{d}={n}" for (h, d), n in why.most_common(10)))
    gone = [j for j, r in zip(candidates, results) if r == "gone"]
    for j in gone[:50]:
        print(f"    gone: {j['company']} | {j['title']} | {j['url']}")
    if LINK_CHECK_MODE != "remove" or not gone:
        if gone:
            print("  [Link check] report-only: nothing removed (set LINK_CHECK_MODE=remove to drop these).")
        return jobs
    gone_ids = {id(j) for j in gone}
    print(f"  [Link check] removed {len(gone_ids)} job(s) whose page is gone.")
    return [j for j in jobs if id(j) not in gone_ids]


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
        "date_added":  datetime.now(timezone.utc).strftime("%Y-%m-%d"),
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
        job_url     = f"{board_url}/OpportunityDetail?opportunityId={opp_id}" if opp_id else board_url
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

    print(f"  [{SOURCE}] Found {len(jobs)} jobs.")
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

    def _first(v):
        if isinstance(v, list):
            return str(v[0]) if v else ""
        return str(v or "")

    country = _first(item.get("countries_derived"))
    parts = [p.strip() for p in _first(item.get("locations_derived")).split(",")]
    state = parts[-2] if len(parts) >= 3 else ""
    meta = _match_org(_load_org_meta(), str(company)) or {}

    return {
        "title":       title,
        "company":     company,
        "location":    location,
        "date_posted": date_posted,
        "url":         url,
        "description": description,
        "job_type":    job_type,
        "source":      item.get("_source") or "Apify",
        "date_added":  datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "job_country": country,
        "job_state":   state,
        "seniority":   "",
        "experience_years": _first(item.get("ai_experience_level")),
        "org_priority": meta.get("priority", ""),
        "org_sector":   meta.get("sector", ""),
        "org_region":   meta.get("region", ""),
        "org_hq":       meta.get("hq", ""),
        "org_size_b":   meta.get("size_b", ""),
    }


# ── CSV output ───────────────────────────────────────────────────────────────

def save_csv(jobs: list[dict]) -> None:
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
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


# ── Seniority + sector labels ────────────────────────────────────────────────
SENIORITY_LEVELS = ["Intern / Entry", "Analyst / Associate", "Senior / Manager",
                    "Director / VP / Principal", "Executive / Head"]

def seniority_label(title: str, years: str = "") -> str:
    """Best-effort seniority from the job title; falls back to Apify's years-of-experience bucket."""
    t = f" {title.lower()} "
    def has(*words): return any(w in t for w in words)
    if has(" intern", "internship", " summer analyst", " co-op", " trainee", " graduate program"):
        return SENIORITY_LEVELS[0]
    if has(" chief ", " cio", " ceo", " cfo", " coo", " president", " head of", " managing director",
           " general partner", " managing partner", " partner,", " partner "):
        return SENIORITY_LEVELS[4]
    if has(" director", " principal", " vice president", " vp,", " vp ", " svp", " evp"):
        return SENIORITY_LEVELS[3]
    if has(" senior", " sr.", " sr ", " lead", " manager", " head "):
        return SENIORITY_LEVELS[2]
    if has(" associate", " analyst", " junior", " entry"):
        return SENIORITY_LEVELS[1]
    return {"0-2": SENIORITY_LEVELS[0], "2-5": SENIORITY_LEVELS[1],
            "5-10": SENIORITY_LEVELS[2], "10+": SENIORITY_LEVELS[3]}.get((years or "").strip(), "Not specified")


_SECTOR_BY_NAME = [
    ("Pension / Retirement", [r"pension", r"retirement", r"ncpers", r"teachers", r"public employees"]),
    ("Endowment / Foundation", [r"endowment", r"foundation", r"university", r"college", r"utimco"]),
    ("Family Office", [r"family office"]),
    ("Sovereign Wealth", [r"sovereign", r"investment authority", r"\bgic\b", r"mubadala", r"temasek"]),
    ("Insurance", [r"insurance", r"assurance"]),
]
_SECTOR_BY_TEXT = [
    ("Real Estate", [r"real estate", r"\breit\b"]),
    ("Private Credit", [r"private credit", r"direct lending"]),
    ("Infrastructure", [r"infrastructure"]),
    ("Venture Capital", [r"venture"]),
    ("Private Equity", [r"private equity", r"buyout"]),
    ("Hedge Fund", [r"hedge fund", r"absolute return"]),
]
_SECTOR_OVERRIDES = None

def _load_sector_overrides() -> dict:
    """data/sector_overrides.csv: company,sector — hand-editable; wins over inference."""
    global _SECTOR_OVERRIDES
    if _SECTOR_OVERRIDES is None:
        _SECTOR_OVERRIDES = {}
        path = pathlib.Path(__file__).parent / "data" / "sector_overrides.csv"
        try:
            with open(path, newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    if r.get("company") and r.get("sector"):
                        _SECTOR_OVERRIDES[r["company"].strip().lower()] = r["sector"].strip()
        except FileNotFoundError:
            pass
    return _SECTOR_OVERRIDES


def sector_label(j: dict) -> str:
    """Sector / asset class: orgs.csv value, then hand-edited overrides, then inference from name, then title/description."""
    if j.get("org_sector"):
        return j["org_sector"]
    company = j.get("company", "").strip().lower()
    if company in _load_sector_overrides():
        return _load_sector_overrides()[company]
    for label, pats in _SECTOR_BY_NAME:
        if any(_re.search(p, company) for p in pats):
            return label
    text = f"{j.get('title', '')} {j.get('description', '')}".lower()
    for label, pats in _SECTOR_BY_TEXT:
        if any(_re.search(p, text) for p in pats):
            return label
    return "Not classified"


def enrich(j: dict) -> dict:
    """Fill seniority and sector labels (also repairs older CSV rows that lack them)."""
    cur = (j.get("seniority") or "").strip()
    if cur and cur[0].isdigit():            # older rows stored the raw Apify bucket here
        j["experience_years"], cur = cur, ""
    if cur not in SENIORITY_LEVELS and cur != "Not specified":
        j["seniority"] = seniority_label(j.get("title", ""), j.get("experience_years", ""))
    j["org_sector"] = sector_label(j)
    return j


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
        source = escape(j.get("source") or "")
        rows.append(
            f'<tr class="age-{age_class}">'
            f'<td>{title_cell}</td>'
            f'<td>{escape(j["company"])}</td>'
            f'<td>{escape(j["location"])}</td>'
            f'<td>{escape(j.get("seniority") or "")}</td>'
            f'<td>{escape(j.get("org_sector") or "")}</td>'
            f'<td data-sort="{escape(raw_date)}">{escape(raw_date)}</td>'
            f'<td>{escape(j["job_type"])}</td>'
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

  /* ── Multi-select filters ── */
  .filter-bar {{ display: flex; gap: .6rem; flex-wrap: wrap; align-items: center;
                 padding: .9rem 1.5rem; border-bottom: 1px solid var(--border); background: var(--cream); }}
  .ms {{ position: relative; }}
  .ms-btn {{ padding: .5rem .9rem; border: 1px solid var(--border); background: var(--white);
             color: var(--navy); font-family: var(--font-body); font-size: .8rem; cursor: pointer; border-radius: 0; }}
  .ms-btn.active {{ border-color: var(--navy); font-weight: 600; }}
  .ms-panel {{ display: none; position: absolute; z-index: 20; top: calc(100% + 2px); left: 0;
               min-width: 240px; max-width: 320px; background: var(--white);
               border: 1px solid var(--navy); box-shadow: 0 6px 18px rgba(0,0,0,.12); }}
  .ms.open .ms-panel {{ display: block; }}
  .ms-search {{ width: 100%; box-sizing: border-box; padding: .5rem .7rem; border: 0;
                border-bottom: 1px solid var(--border); font-size: .8rem; outline: none; font-family: var(--font-body); }}
  .ms-list {{ max-height: 260px; overflow-y: auto; padding: .3rem 0; }}
  .ms-opt {{ display: flex; gap: .5rem; align-items: center; padding: .3rem .8rem; font-size: .8rem; cursor: pointer; }}
  .ms-opt:hover {{ background: var(--cream); }}
  .ms-foot {{ padding: .4rem .8rem; border-top: 1px solid var(--border); font-size: .75rem; }}
  .ms-foot a {{ color: var(--navy); cursor: pointer; text-decoration: underline; }}
  #clearAll {{ font-size: .78rem; color: var(--navy); cursor: pointer; text-decoration: underline; background: none; border: 0; }}
</style>
</head>
<body>

<!-- ── Email gate ── -->
<div id="gate" style="position:fixed;inset:0;background:var(--navy);display:flex;align-items:center;justify-content:center;z-index:999;">
  <div style="background:#fff;padding:2.5rem 2rem;width:360px;text-align:center;">
    <img src="https://framcompany.com/assets/fram-logo-BYQ0yDsY.png" alt="FRAM Partners" style="height:30px;margin-bottom:1.5rem;display:block;margin-left:auto;margin-right:auto;" />
    <h2 style="font-family:var(--font-display);font-size:1.3rem;font-weight:400;color:var(--navy);margin-bottom:.4rem;">Allocator Job Board</h2>
    <p style="font-size:.8rem;color:var(--muted);margin-bottom:1.5rem;">Enter your details to access the board.</p>
    <input id="gate-name" type="text" placeholder="Full name" style="width:100%;padding:.6rem .8rem;border:1px solid var(--border);font-family:var(--font-body);font-size:.88rem;margin-bottom:.6rem;outline:none;box-sizing:border-box;" />
    <input id="gate-email" type="email" placeholder="Email address" style="width:100%;padding:.6rem .8rem;border:1px solid var(--border);font-family:var(--font-body);font-size:.88rem;margin-bottom:.8rem;outline:none;box-sizing:border-box;" />
    <button onclick="submitGate()" style="width:100%;padding:.65rem;background:var(--navy);color:#fff;border:none;font-family:var(--font-body);font-size:.85rem;font-weight:500;cursor:pointer;letter-spacing:.04em;">View Jobs</button>
    <div id="gate-error" style="color:var(--burgundy);font-size:.78rem;margin-top:.6rem;display:none;"></div>
  </div>
</div>

<script>
const APPS_SCRIPT_URL = "https://script.google.com/macros/s/AKfycbyBlYbwlbXIhjWwiPeAcw4hRQkSMAW7nolA9XLZsYkMfiou-So5sSQqiYCrtN276bbOlw/exec";

// Skip gate if already submitted
if (localStorage.getItem("fram_gate_passed") === "1") {{
  document.getElementById("gate").style.display = "none";
}}

async function submitGate() {{
  const name  = document.getElementById("gate-name").value.trim();
  const email = document.getElementById("gate-email").value.trim();
  const err   = document.getElementById("gate-error");
  if (!name) {{ err.textContent = "Please enter your name."; err.style.display = "block"; return; }}
  if (!email || !email.includes("@")) {{ err.textContent = "Please enter a valid email."; err.style.display = "block"; return; }}
  err.style.display = "none";

  // Show the board immediately — don't make them wait for the sheet write
  document.getElementById("gate").style.display = "none";
  localStorage.setItem("fram_gate_passed", "1");

  // Fire-and-forget to Google Sheet
  fetch(APPS_SCRIPT_URL, {{
    method: "POST",
    body: JSON.stringify({{ name, email }}),
  }}).catch(() => {{}});
}}

document.getElementById("gate-name").addEventListener("keydown", e => {{ if (e.key === "Enter") document.getElementById("gate-email").focus(); }});
document.getElementById("gate-email").addEventListener("keydown", e => {{ if (e.key === "Enter") submitGate(); }});
</script>

<header>
  <div class="header-inner">
    <img src="https://raw.githubusercontent.com/kamitchell588/allocator-jobs/main/Fram_F_Logo_WhitePNG%20(1).png" alt="FRAM Partners" style="height:36px;margin-bottom:1.25rem;display:block;" />
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
      </div>
    </div>
    <div class="filter-bar" id="filterBar"></div>
    <div class="table-wrap">
      <table id="jobsTable">
        <thead>
          <tr>
            <th data-col="0">Title <span class="sort-icon"></span></th>
            <th data-col="1">Company <span class="sort-icon"></span></th>
            <th data-col="2">Location <span class="sort-icon"></span></th>
            <th data-col="3">Seniority <span class="sort-icon"></span></th>
            <th data-col="4">Sector <span class="sort-icon"></span></th>
            <th data-col="5">Date Posted <span class="sort-icon"></span></th>
            <th data-col="6">Type <span class="sort-icon"></span></th>
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

// ── Search + multi-select filters ──
const searchBox = document.getElementById("searchBox");
const tableBody = document.getElementById("tableBody");
const noResults = document.getElementById("noResults");
const filterBar = document.getElementById("filterBar");

const regionLabel = {{ us: "United States", canada: "Canada", other: "Other / Unknown" }};
const SENIORITY_ORDER = ["Intern / Entry","Analyst / Associate","Senior / Manager","Director / VP / Principal","Executive / Head","Not specified"];

// Each filter: label, how to read a value list from a row, optional fixed option order
const FILTERS = [
  {{ key: "company",   label: "Company",   get: r => [r.cells[1].textContent.trim()] }},
  {{ key: "region",    label: "Region",    get: r => [regionLabel[classifyRegion(r.cells[2].textContent)]] }},
  {{ key: "seniority", label: "Seniority", get: r => [r.cells[3].textContent.trim() || "Not specified"], order: SENIORITY_ORDER }},
  {{ key: "sector",    label: "Sector",    get: r => [r.cells[4].textContent.trim() || "Not classified"] }},
  {{ key: "type",      label: "Type",      get: r => (r.cells[6].textContent.split(",").map(x => x.trim()).filter(Boolean)) }},
];
const selected = {{}};   // key -> Set of chosen values (empty = no restriction)

function buildFilters() {{
  const rows = Array.from(tableBody.rows);
  FILTERS.forEach(f => {{
    selected[f.key] = new Set();
    const counts = {{}};
    rows.forEach(r => {{ f.get(r).forEach(v => {{ if (v) counts[v] = (counts[v] || 0) + 1; }}); }});
    let opts = Object.keys(counts);
    opts = f.order ? opts.filter(o => f.order.includes(o)).sort((a, b) => f.order.indexOf(a) - f.order.indexOf(b))
                   : opts.sort((a, b) => a.localeCompare(b));
    if (!opts.length) return;
    const wrap = document.createElement("div"); wrap.className = "ms";
    const btn = document.createElement("button"); btn.type = "button"; btn.className = "ms-btn";
    const panel = document.createElement("div"); panel.className = "ms-panel";
    let search = null;
    if (opts.length > 8) {{
      search = document.createElement("input"); search.className = "ms-search"; search.placeholder = "Search " + f.label.toLowerCase() + "…";
      panel.appendChild(search);
    }}
    const list = document.createElement("div"); list.className = "ms-list";
    opts.forEach(o => {{
      const lab = document.createElement("label"); lab.className = "ms-opt"; lab.dataset.v = o.toLowerCase();
      const cb = document.createElement("input"); cb.type = "checkbox";
      cb.addEventListener("change", () => {{
        cb.checked ? selected[f.key].add(o) : selected[f.key].delete(o);
        updateBtn(); filterTable();
      }});
      lab.appendChild(cb); lab.appendChild(document.createTextNode(o + " (" + counts[o] + ")"));
      lab._cb = cb; list.appendChild(lab);
    }});
    panel.appendChild(list);
    const foot = document.createElement("div"); foot.className = "ms-foot";
    const clr = document.createElement("a"); clr.textContent = "Clear";
    clr.addEventListener("click", () => {{ selected[f.key].clear(); list.querySelectorAll("input").forEach(c => c.checked = false); updateBtn(); filterTable(); }});
    foot.appendChild(clr); panel.appendChild(foot);
    function updateBtn() {{
      const n = selected[f.key].size;
      btn.textContent = f.label + (n ? " (" + n + ")" : "") + " ▾";
      btn.classList.toggle("active", n > 0);
    }}
    updateBtn();
    btn.addEventListener("click", e => {{
      e.stopPropagation();
      document.querySelectorAll(".ms.open").forEach(m => {{ if (m !== wrap) m.classList.remove("open"); }});
      wrap.classList.toggle("open");
    }});
    panel.addEventListener("click", e => e.stopPropagation());
    if (search) search.addEventListener("input", () => {{
      const q = search.value.toLowerCase();
      list.querySelectorAll(".ms-opt").forEach(l => l.style.display = l.dataset.v.includes(q) ? "" : "none");
    }});
    wrap.appendChild(btn); wrap.appendChild(panel); filterBar.appendChild(wrap);
    f._reset = () => {{ selected[f.key].clear(); list.querySelectorAll("input").forEach(c => c.checked = false); updateBtn(); }};
  }});
  const all = document.createElement("button"); all.type = "button"; all.id = "clearAll"; all.textContent = "Clear all filters";
  all.addEventListener("click", () => {{ FILTERS.forEach(f => f._reset && f._reset()); searchBox.value = ""; filterTable(); }});
  filterBar.appendChild(all);
  document.addEventListener("click", () => document.querySelectorAll(".ms.open").forEach(m => m.classList.remove("open")));
}}

function filterTable() {{
  const q = searchBox.value.toLowerCase().trim();
  let visible = 0;
  Array.from(tableBody.rows).forEach(row => {{
    let show = !q || row.textContent.toLowerCase().includes(q);
    if (show) {{
      for (const f of FILTERS) {{
        const sel = selected[f.key];
        if (sel && sel.size && !f.get(row).some(v => sel.has(v))) {{ show = false; break; }}
      }}
    }}
    row.style.display = show ? "" : "none";
    if (show) visible++;
  }});
  noResults.style.display = visible === 0 ? "block" : "none";
}}

searchBox.addEventListener("input", filterTable);
buildFilters();

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


def generate_admin_html(jobs: list[dict]) -> None:
    hidden  = load_hidden_jobs()
    updated = datetime.now().strftime("%B %d, %Y at %I:%M %p")

    rows = []
    for j in jobs:
        raw_date  = j["date_posted"] or ""
        url       = escape(j["url"])
        title_cell = (
            f'<a href="{url}" target="_blank" rel="noopener">{escape(j["title"])}</a>'
            if url else escape(j["title"])
        )
        source  = escape(j.get("source") or "")
        is_hidden = j["url"].lower().rstrip("/") in hidden
        row_style = ' style="opacity:.4"' if is_hidden else ""
        hide_btn  = (
            f'<button class="hide-btn" data-url="{url}" onclick="hideJob(this)">'
            f'{"Unhide" if is_hidden else "Hide"}</button>'
        )
        date_added = escape(j.get("date_added") or "")
        rows.append(
            f'<tr{row_style}>'
            f'<td>{title_cell}</td>'
            f'<td>{escape(j["company"])}</td>'
            f'<td>{escape(j["location"])}</td>'
            f'<td>{escape(raw_date)}</td>'
            f'<td>{date_added}</td>'
            f'<td>{escape(j["job_type"])}</td>'
            f'<td><span class="source-tag">{source}</span></td>'
            f'<td>{hide_btn}</td>'
            f'</tr>'
        )
    rows_html = "\n".join(rows)

    admin_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Admin — Allocator Jobs</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Source+Serif+4:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root {{
    --navy:     #1b2232;
    --burgundy: #4f1722;
    --cream:    #f9f5f0;
    --border:   #e3e5e8;
    --muted:    #676f7e;
    --font-display: "Source Serif 4", Georgia, serif;
    --font-body:    "Inter", system-ui, sans-serif;
  }}
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: var(--font-body); background: var(--cream); color: var(--navy); min-height: 100vh; }}
  a {{ color: var(--burgundy); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}

  /* Password gate */
  #gate {{
    position: fixed; inset: 0; background: var(--navy);
    display: flex; align-items: center; justify-content: center; z-index: 999;
  }}
  #gate-box {{
    background: #fff; padding: 2.5rem 2rem; width: 340px; text-align: center;
  }}
  #gate-box h2 {{ font-family: var(--font-display); font-size: 1.4rem; font-weight: 400; margin-bottom: .4rem; color: var(--navy); }}
  #gate-box p  {{ font-size: .8rem; color: var(--muted); margin-bottom: 1.5rem; }}
  #gate-box input {{
    width: 100%; padding: .6rem .8rem; border: 1px solid var(--border);
    font-family: var(--font-body); font-size: .9rem; margin-bottom: .8rem; outline: none;
  }}
  #gate-box input:focus {{ border-color: var(--navy); }}
  #gate-box button {{
    width: 100%; padding: .65rem; background: var(--navy); color: #fff;
    border: none; font-family: var(--font-body); font-size: .85rem;
    font-weight: 500; cursor: pointer; letter-spacing: .04em;
  }}
  #gate-error {{ color: var(--burgundy); font-size: .8rem; margin-top: .6rem; display: none; }}

  /* Header */
  header {{
    background: var(--navy); color: #fff;
    padding: 2rem 2rem 1.5rem;
    border-bottom: 3px solid var(--burgundy);
  }}
  header h1 {{ font-family: var(--font-display); font-size: 1.6rem; font-weight: 400; }}
  header .sub {{ font-size: .75rem; color: #a0aec0; margin-top: .3rem; }}
  .admin-badge {{
    display: inline-block; background: var(--burgundy); color: #fff;
    font-size: .65rem; font-weight: 600; letter-spacing: .08em;
    padding: .2rem .5rem; margin-left: .6rem; vertical-align: middle;
  }}

  /* Main */
  main {{ max-width: 1300px; margin: 0 auto; padding: 1.5rem 2rem; }}

  /* Notice */
  .notice {{
    background: #fff8e1; border-left: 4px solid #f59e0b;
    padding: .8rem 1rem; font-size: .82rem; margin-bottom: 1.5rem; color: #92400e;
  }}
  .notice code {{
    background: #fef3c7; padding: .1rem .3rem; font-size: .8rem; font-family: monospace;
  }}

  /* Toolbar */
  .toolbar {{ display: flex; gap: .6rem; align-items: center; margin-bottom: 1rem; flex-wrap: wrap; }}
  .toolbar input {{
    flex: 1; min-width: 200px; padding: .45rem .8rem;
    border: 1px solid var(--border); font-family: var(--font-body); font-size: .85rem; outline: none;
  }}
  .toolbar input:focus {{ border-color: var(--navy); }}

  /* Table */
  .table-wrap {{ overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; background: #fff; font-size: .82rem; }}
  th {{ background: var(--navy); color: #fff; padding: .6rem .8rem; text-align: left; font-weight: 500; white-space: nowrap; }}
  td {{ padding: .55rem .8rem; border-bottom: 1px solid var(--border); vertical-align: top; }}
  tr:hover td {{ background: var(--cream); }}

  .source-tag {{
    display: inline-block; padding: .15rem .5rem;
    font-size: .68rem; font-weight: 500;
    background: #eef2ff; border: 1px solid #c7d2fe;
    color: var(--navy); white-space: nowrap;
  }}

  /* Sortable headers */
  th[data-col] {{ user-select: none; }}
  th[data-col]:hover {{ background: #24304a; }}
  th[data-col].asc  .sort-icon::after {{ content: " ▲"; opacity: .7; }}
  th[data-col].desc .sort-icon::after {{ content: " ▼"; opacity: .7; }}
  th[data-col] .sort-icon {{ font-size: .65rem; }}

  /* Hide button */
  .hide-btn {{
    padding: .25rem .7rem; font-size: .75rem; font-family: var(--font-body);
    cursor: pointer; border: 1px solid var(--burgundy); background: #fff;
    color: var(--burgundy); font-weight: 500; white-space: nowrap;
  }}
  .hide-btn:hover {{ background: var(--burgundy); color: #fff; }}

  /* Add Job by URL panel */
  .add-job-panel {{
    background: #fff; border: 1px solid var(--border);
    padding: 1.2rem 1.4rem; margin-bottom: 1.5rem;
  }}
  .add-job-panel h3 {{
    font-family: var(--font-display); font-size: 1rem; font-weight: 500;
    margin-bottom: .8rem; color: var(--navy);
  }}
  .add-job-row {{ display: flex; gap: .6rem; align-items: center; flex-wrap: wrap; }}
  .add-job-row input[type="url"] {{
    flex: 1; min-width: 260px; padding: .45rem .8rem;
    border: 1px solid var(--border); font-family: var(--font-body);
    font-size: .85rem; outline: none;
  }}
  .add-job-row input[type="url"]:focus {{ border-color: var(--navy); }}
  .add-job-row button {{
    padding: .45rem 1.2rem; background: var(--burgundy); color: #fff;
    border: none; font-family: var(--font-body); font-size: .82rem;
    font-weight: 500; cursor: pointer; letter-spacing: .04em; white-space: nowrap;
  }}
  .add-job-row button:disabled {{ opacity: .6; cursor: default; }}
  #addJobStatus {{ font-size: .78rem; color: var(--muted); margin-top: .5rem; }}

  /* Hidden URL panel */
  #hidden-panel {{
    display: none; position: fixed; bottom: 1.5rem; right: 1.5rem;
    background: var(--navy); color: #fff; padding: 1rem 1.2rem;
    max-width: 480px; font-size: .8rem; z-index: 100;
  }}
  #hidden-panel h4 {{ font-size: .85rem; margin-bottom: .5rem; }}
  #hidden-panel textarea {{
    width: 100%; height: 80px; font-family: monospace; font-size: .75rem;
    padding: .4rem; background: #0f172a; color: #a3e635; border: none; resize: none;
  }}
  #hidden-panel p {{ margin-top: .5rem; color: #94a3b8; font-size: .75rem; }}
  #hidden-panel button {{
    margin-top: .6rem; padding: .3rem .8rem; background: var(--burgundy);
    color: #fff; border: none; font-family: var(--font-body); font-size: .75rem; cursor: pointer;
  }}
</style>
</head>
<body>

<!-- Password gate -->
<div id="gate">
  <div id="gate-box">
    <h2>Admin Access</h2>
    <p>FRAM Partners — Internal Use Only</p>
    <input type="password" id="pw-input" placeholder="Password" onkeydown="if(event.key==='Enter')checkPw()" />
    <input type="password" id="token-input" placeholder="GitHub token" onkeydown="if(event.key==='Enter')checkPw()" style="margin-top:0" />
    <input type="password" id="anthropic-input" placeholder="Anthropic API key" onkeydown="if(event.key==='Enter')checkPw()" style="margin-top:.5rem" />
    <button onclick="checkPw()">Enter</button>
    <div id="gate-error">Incorrect password</div>
  </div>
</div>

<header>
  <h1>Allocator Jobs <span class="admin-badge">ADMIN</span></h1>
  <div class="sub">Last updated: {updated} &nbsp;·&nbsp; {len(jobs)} total jobs (including hidden)</div>
</header>

<main>
  <div class="notice">
    Click <strong>Hide</strong> on any row to remove it from the public dashboard — it will update automatically in ~2 minutes.
    Click <strong>Unhide</strong> to restore it.
  </div>

  <!-- Add Job by URL -->
  <div class="add-job-panel">
    <h3>Add Job by URL</h3>
    <div class="add-job-row">
      <input type="url" id="jobUrlInput" placeholder="Paste job posting URL…" />
      <button id="addJobBtn" onclick="addJobByUrl()">Submit</button>
    </div>
    <div id="addJobStatus"></div>
    <div id="jobConfirmForm" style="display:none;margin-top:1rem;border-top:1px solid var(--border);padding-top:1rem;">
      <p style="font-size:.78rem;color:var(--muted);margin-bottom:.8rem;">Review and fill in any missing fields, then click Save.</p>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:.6rem;">
        <div>
          <label style="font-size:.72rem;font-weight:500;color:var(--muted);display:block;margin-bottom:.2rem;">Job Title *</label>
          <input type="text" id="cfTitle" style="width:100%;padding:.4rem .6rem;border:1px solid var(--border);font-family:var(--font-body);font-size:.82rem;outline:none;" />
        </div>
        <div>
          <label style="font-size:.72rem;font-weight:500;color:var(--muted);display:block;margin-bottom:.2rem;">Company *</label>
          <input type="text" id="cfCompany" style="width:100%;padding:.4rem .6rem;border:1px solid var(--border);font-family:var(--font-body);font-size:.82rem;outline:none;" />
        </div>
        <div>
          <label style="font-size:.72rem;font-weight:500;color:var(--muted);display:block;margin-bottom:.2rem;">Location</label>
          <input type="text" id="cfLocation" placeholder="e.g. New York, NY" style="width:100%;padding:.4rem .6rem;border:1px solid var(--border);font-family:var(--font-body);font-size:.82rem;outline:none;" />
        </div>
        <div>
          <label style="font-size:.72rem;font-weight:500;color:var(--muted);display:block;margin-bottom:.2rem;">Date Posted</label>
          <input type="text" id="cfDate" placeholder="YYYY-MM-DD" style="width:100%;padding:.4rem .6rem;border:1px solid var(--border);font-family:var(--font-body);font-size:.82rem;outline:none;" />
        </div>
      </div>
      <div style="margin-top:.8rem;display:flex;gap:.6rem;">
        <button onclick="confirmAddJob()" style="padding:.4rem 1.2rem;background:var(--navy);color:#fff;border:none;font-family:var(--font-body);font-size:.82rem;font-weight:500;cursor:pointer;">Save Job</button>
        <button onclick="cancelAddJob()" style="padding:.4rem 1rem;background:#fff;color:var(--muted);border:1px solid var(--border);font-family:var(--font-body);font-size:.82rem;cursor:pointer;">Cancel</button>
      </div>
    </div>
  </div>

  <div class="toolbar">
    <input type="text" id="adminSearch" placeholder="Search jobs…" oninput="filterAdmin()" />
    <button id="refreshBtn" onclick="triggerRefresh()" style="padding:.45rem 1.2rem;background:var(--navy);color:#fff;border:none;font-family:var(--font-body);font-size:.82rem;font-weight:500;cursor:pointer;letter-spacing:.04em;white-space:nowrap;">
      ↻ Refresh Jobs
    </button>
    <span id="refreshStatus" style="font-size:.78rem;color:var(--muted);"></span>
  </div>

  <div class="table-wrap">
    <table id="adminTable">
      <thead>
        <tr>
          <th data-col="0" style="cursor:pointer;" onclick="sortAdmin(0)">Title <span class="sort-icon"></span></th>
          <th data-col="1" style="cursor:pointer;" onclick="sortAdmin(1)">Company <span class="sort-icon"></span></th>
          <th data-col="2" style="cursor:pointer;" onclick="sortAdmin(2)">Location <span class="sort-icon"></span></th>
          <th data-col="3" style="cursor:pointer;" onclick="sortAdmin(3)">Date Posted <span class="sort-icon"></span></th>
          <th data-col="4" style="cursor:pointer;" onclick="sortAdmin(4)">Date Added <span class="sort-icon"></span></th>
          <th data-col="5">Type</th>
          <th data-col="6">Source</th>
          <th>Action</th>
        </tr>
      </thead>
      <tbody id="adminBody">
        {rows_html}
      </tbody>
    </table>
  </div>
</main>

<!-- Toast notification -->
<div id="toast" style="display:none;position:fixed;bottom:1.5rem;right:1.5rem;background:var(--navy);color:#fff;padding:.8rem 1.2rem;font-size:.82rem;font-family:var(--font-body);z-index:200;max-width:360px;"></div>

<script>
// ── Password gate ──
const ADMIN_PW = "fram2024";
function checkPw() {{
  const val   = document.getElementById("pw-input").value;
  const token = document.getElementById("token-input").value.trim();
  if (val === ADMIN_PW) {{
    document.getElementById("gate").style.display = "none";
    sessionStorage.setItem("admin_auth", "1");
    if (token) sessionStorage.setItem("gh_token", token);
    const anthropicKey = document.getElementById("anthropic-input").value.trim();
    if (anthropicKey) sessionStorage.setItem("anthropic_key", anthropicKey);
  }} else {{
    document.getElementById("gate-error").style.display = "block";
  }}
}}
// Auto-unlock if already authenticated this session AND Anthropic key is present
if (sessionStorage.getItem("admin_auth") === "1" && sessionStorage.getItem("anthropic_key")) {{
  document.getElementById("gate").style.display = "none";
}}

// ── Search ──
function filterAdmin() {{
  const q = document.getElementById("adminSearch").value.toLowerCase();
  Array.from(document.getElementById("adminBody").rows).forEach(row => {{
    row.style.display = !q || row.textContent.toLowerCase().includes(q) ? "" : "none";
  }});
}}

// ── Toast helper ──
function showToast(msg, color) {{
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.style.background = color || "var(--navy)";
  t.style.display = "block";
  setTimeout(() => t.style.display = "none", 4000);
}}

// ── GitHub API helpers ──
const REPO = "kamitchell588/allocator-jobs";
function ghHeaders() {{
  return {{
    "Authorization": "Bearer " + (sessionStorage.getItem("gh_token") || ""),
    "Accept": "application/vnd.github+json",
    "Content-Type": "application/json",
  }};
}}

async function getFileSha() {{
  const r = await fetch(`https://api.github.com/repos/${{REPO}}/contents/hidden_jobs.txt`, {{ headers: ghHeaders() }});
  if (!r.ok) return {{ sha: null, content: "" }};
  const d = await r.json();
  return {{ sha: d.sha, content: atob(d.content.replace(/\\n/g,"")) }};
}}

async function updateHiddenFile(newContent, sha) {{
  const body = {{ message: "Update hidden jobs", content: btoa(newContent), branch: "main" }};
  if (sha) body.sha = sha;
  const r = await fetch(`https://api.github.com/repos/${{REPO}}/contents/hidden_jobs.txt`, {{
    method: "PUT", headers: ghHeaders(), body: JSON.stringify(body),
  }});
  return r.ok;
}}

async function triggerWorkflow() {{
  const r = await fetch(
    `https://api.github.com/repos/${{REPO}}/actions/workflows/refresh.yml/dispatches`,
    {{ method: "POST", headers: ghHeaders(), body: JSON.stringify({{ ref: "main" }}) }}
  );
  return r.status === 204;
}}

// ── Hide job (fully automatic) ──
async function hideJob(btn) {{
  const url = btn.dataset.url;
  const row = btn.closest("tr");
  const hiding = btn.textContent.trim() === "Hide";
  btn.disabled = true;
  btn.textContent = "…";

  const {{ sha, content }} = await getFileSha();
  const lines = content.split("\\n").map(l => l.trim()).filter(l => l && !l.startsWith("#"));

  let newLines;
  if (hiding) {{
    if (!lines.includes(url)) lines.push(url);
    newLines = lines;
  }} else {{
    newLines = lines.filter(l => l !== url);
  }}

  const ok = await updateHiddenFile(newLines.join("\\n") + (newLines.length ? "\\n" : ""), sha);
  if (!ok) {{
    showToast("Failed to update hidden_jobs.txt — check your token.", "#b91c1c");
    btn.disabled = false;
    btn.textContent = hiding ? "Hide" : "Unhide";
    return;
  }}

  row.style.opacity = hiding ? "0.4" : "";
  btn.textContent   = hiding ? "Unhide" : "Hide";
  btn.disabled      = false;

  // Trigger a dashboard refresh automatically
  const triggered = await triggerWorkflow();
  showToast(
    hiding
      ? (triggered ? "✓ Job hidden — dashboard refreshing (~2 min)" : "✓ Job hidden (refresh failed — try the Refresh button)")
      : (triggered ? "✓ Job restored — dashboard refreshing (~2 min)" : "✓ Job restored (refresh failed — try the Refresh button)"),
    hiding ? "#1b2232" : "#166534"
  );
}}

// ── Column sort ──
let _sortCol = -1, _sortAsc = true;
function sortAdmin(col) {{
  if (_sortCol === col) {{ _sortAsc = !_sortAsc; }} else {{ _sortCol = col; _sortAsc = true; }}
  document.querySelectorAll("th[data-col]").forEach(th => {{
    th.classList.remove("asc", "desc");
    if (parseInt(th.dataset.col) === col) th.classList.add(_sortAsc ? "asc" : "desc");
  }});
  const tbody = document.getElementById("adminBody");
  Array.from(tbody.rows)
    .sort((a, b) => {{
      const ta = (a.cells[col]?.textContent || "").trim().toLowerCase();
      const tb = (b.cells[col]?.textContent || "").trim().toLowerCase();
      return _sortAsc ? ta.localeCompare(tb) : tb.localeCompare(ta);
    }})
    .forEach(r => tbody.appendChild(r));
}}

// ── Add Job by URL ──

async function addJobByUrl() {{
  const urlInput = document.getElementById("jobUrlInput");
  const statusEl = document.getElementById("addJobStatus");
  const btn      = document.getElementById("addJobBtn");
  const jobUrl   = urlInput.value.trim();

  if (!jobUrl) {{ statusEl.textContent = "Please enter a URL."; return; }}

  btn.disabled = true;
  statusEl.textContent = "Fetching job page…";

  let pageText = "";
  const proxies = [
    u => `https://api.allorigins.win/get?url=${{encodeURIComponent(u)}}`,
    u => `https://corsproxy.io/?${{encodeURIComponent(u)}}`,
  ];
  for (const makeUrl of proxies) {{
    try {{
      const proxyResp = await fetch(makeUrl(jobUrl), {{ signal: AbortSignal.timeout(8000) }});
      if (!proxyResp.ok) continue;
      const raw = await proxyResp.text();
      let html = raw;
      try {{ html = JSON.parse(raw).contents || raw; }} catch (_) {{}}
      pageText = html
        .replace(/<style[\s\S]*?<\/style>/gi, "")
        .replace(/<script[\s\S]*?<\/script>/gi, "")
        .replace(/<[^>]+>/g, " ")
        .replace(/\s+/g, " ")
        .trim()
        .slice(0, 12000);
      if (pageText.length > 200) break;
    }} catch (e) {{ console.warn("Proxy failed:", e.message); }}
  }}

  statusEl.textContent = "Parsing with Claude…";
  let job = {{}};
  try {{
    const prompt = pageText
      ? `Here is the text scraped from this job posting (${{jobUrl}}):\\n\\n${{pageText}}\\n\\nReturn ONLY a JSON object with: title, company, location (city + state/country), date_posted (YYYY-MM-DD or null), type (FULL_TIME/PART_TIME or null).`
      : `Job URL: ${{jobUrl}}\\nReturn ONLY a JSON object with: title, company, location, date_posted, type. Use null for fields you cannot determine.`;
    const resp = await fetch("https://api.anthropic.com/v1/messages", {{
      method: "POST",
      headers: {{
        "x-api-key": sessionStorage.getItem("anthropic_key") || "",
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
        "anthropic-dangerous-direct-browser-access": "true",
      }},
      body: JSON.stringify({{
        model: "claude-haiku-4-5-20251001",
        max_tokens: 256,
        messages: [{{ role: "user", content: prompt }}]
      }})
    }});
    if (!resp.ok) {{
      const errBody = await resp.json().catch(() => ({{}}));
      throw new Error(`Anthropic API error: ${{resp.status}} — ${{errBody.error?.message || JSON.stringify(errBody)}}`);
    }}
    const data = await resp.json();
    const text = data.content?.[0]?.text || "";
    const match = text.match(/\{{[\s\S]*\}}/);
    if (match) job = JSON.parse(match[0]);
  }} catch (err) {{
    statusEl.textContent = "Error parsing job: " + err.message;
    btn.disabled = false;
    return;
  }}

  if (!job.title) {{
    statusEl.textContent = "Could not extract job title. Try a different URL.";
    btn.disabled = false;
    return;
  }}

  document.getElementById("cfTitle").value    = job.title    || "";
  document.getElementById("cfCompany").value  = job.company  || "";
  document.getElementById("cfLocation").value = job.location || "";
  document.getElementById("cfDate").value     = job.date_posted || "";
  document.getElementById("jobConfirmForm").style.display = "block";
  statusEl.textContent = pageText.length > 200
    ? "Review the details below and fill in anything missing."
    : "Job board blocked scraping — please fill in the missing fields.";
  btn.disabled = false;
}}

async function confirmAddJob() {{
  const jobUrl      = document.getElementById("jobUrlInput").value.trim();
  const title       = document.getElementById("cfTitle").value.trim();
  const company     = document.getElementById("cfCompany").value.trim();
  const location    = document.getElementById("cfLocation").value.trim();
  const date_posted = document.getElementById("cfDate").value.trim();
  const statusEl    = document.getElementById("addJobStatus");

  if (!title) {{ statusEl.textContent = "Job title is required."; return; }}

  statusEl.textContent = "Saving to allocator_jobs.csv…";

  try {{
    const csvResp = await fetch(`https://api.github.com/repos/${{REPO}}/contents/allocator_jobs.csv`, {{ headers: ghHeaders() }});
    if (!csvResp.ok) throw new Error(`GitHub fetch error: ${{csvResp.status}}`);
    const csvData = await csvResp.json();
    const existing = atob(csvData.content.replace(/\\n/g, ""));
    function csvEsc(v) {{ return /[",\\n]/.test(v) ? `"${{v.replace(/"/g,'""')}}"` : v; }}
    const newRow = [csvEsc(title), csvEsc(company), csvEsc(location), date_posted, csvEsc(jobUrl), "", "Manual"].join(",") + "\\n";
    const putResp = await fetch(`https://api.github.com/repos/${{REPO}}/contents/allocator_jobs.csv`, {{
      method: "PUT",
      headers: ghHeaders(),
      body: JSON.stringify({{
        message: `Add manual job: ${{title}}`,
        content: btoa(unescape(encodeURIComponent(existing + newRow))),
        sha: csvData.sha,
        branch: "main"
      }})
    }});
    if (!putResp.ok) {{
      const errBody = await putResp.json().catch(() => ({{}}));
      throw new Error(errBody.message || `GitHub write error: ${{putResp.status}}`);
    }}
  }} catch (err) {{
    statusEl.textContent = "Error saving to CSV: " + err.message;
    return;
  }}

  const tbody = document.getElementById("adminBody");
  const tr = document.createElement("tr");
  const safeUrl = jobUrl.replace(/"/g, "&quot;");
  tr.innerHTML = `
    <td><a href="${{safeUrl}}" target="_blank" rel="noopener">${{title}}</a></td>
    <td>${{company}}</td>
    <td>${{location}}</td>
    <td>${{date_posted}}</td>
    <td></td>
    <td><span class="source-tag">Manual</span></td>
    <td><button class="hide-btn" data-url="${{safeUrl}}" onclick="hideJob(this)">Hide</button></td>
  `;
  tbody.insertBefore(tr, tbody.firstChild);

  document.getElementById("jobUrlInput").value = "";
  document.getElementById("jobConfirmForm").style.display = "none";
  statusEl.textContent = "";
  showToast("✓ Job added successfully!", "#166534");
}}

function cancelAddJob() {{
  document.getElementById("jobConfirmForm").style.display = "none";
  document.getElementById("addJobStatus").textContent = "";
  document.getElementById("addJobBtn").disabled = false;
}}

// ── Manual Refresh Jobs button ──
async function triggerRefresh() {{
  const btn    = document.getElementById("refreshBtn");
  const status = document.getElementById("refreshStatus");
  btn.disabled = true;
  btn.textContent = "Triggering…";
  status.textContent = "";
  const ok = await triggerWorkflow();
  if (ok) {{
    status.textContent = "✓ Refresh started — dashboard will update in ~2 minutes.";
    status.style.color = "#16a34a";
  }} else {{
    status.textContent = "Error triggering refresh — check your token.";
    status.style.color = "var(--burgundy)";
  }}
  btn.disabled = false;
  btn.textContent = "↻ Refresh Jobs";
}}
</script>
</body>
</html>
"""

    with open(ADMIN_PATH, "w", encoding="utf-8") as f:
        f.write(admin_html)
    print(f"Admin dashboard saved → {ADMIN_PATH}")


# ── Main ─────────────────────────────────────────────────────────────────────

def load_hidden_jobs() -> set:
    """Return a set of lowercased URLs that should be suppressed from the public dashboard."""
    p = pathlib.Path(HIDDEN_JOBS_PATH)
    if not p.exists():
        return set()
    return {line.strip().lower().rstrip("/") for line in p.read_text().splitlines() if line.strip() and not line.startswith("#")}


def _url_key(url: str) -> str:
    return url.lower().rstrip("/") if url else ""


def _dedup(jobs: list[dict]) -> list[dict]:
    """Remove duplicates. The job link is the identity. Only a job with no link falls back to
    title + company + location (so the same title in two cities is kept)."""
    seen = set()
    unique = []
    for j in jobs:
        key = _url_key(j["url"]) or (j["title"].lower().strip(), j["company"].lower().strip(),
                                     (j.get("location") or "").lower().strip())
        if key in seen:
            continue
        seen.add(key)
        unique.append(j)
    return unique


NON_APIFY_SOURCES = ("Manual", "NCPERS", "AllocatorJobs", "UTIMCO")


def _job_key(j: dict):
    return _url_key(j["url"]) or (j["title"].lower().strip(), j["company"].lower().strip())


def _previous_active_count() -> int:
    """Jobs on the public board as of the last saved CSV (rows not marked hidden)."""
    try:
        with open(CSV_PATH, newline="", encoding="utf-8") as f:
            return sum(1 for r in csv.DictReader(f) if r.get("status", "") not in ("hidden", "expired"))
    except OSError:
        return 0


SHRINK_LIMIT = 0.30   # stop if the board would lose more than this share of its jobs in one run


def main():
    print("=" * 60)
    print("  Institutional Allocator Job Scraper")
    print("=" * 60)

    # ── 1. Load historical jobs from CSV + fetch last 24 hours from Apify ──
    print("\n[Apify] Fetching last 24 hours …")
    recent_raw  = fetch_recent_items()
    recent_kept = filter_jobs(recent_raw) if recent_raw else []
    recent_jobs = [normalise(j) for j in recent_kept]
    write_run_report(recent_raw, recent_kept)
    print(f"  [Apify] {len(recent_jobs)} new/recent jobs after filtering.")

    # Backfill from any one-time datasets (cleared after use)
    if FALLBACK_DATASET_IDS:
        print("\n[Apify] Fetching backfill datasets …")
        backfill_raw  = fetch_one_time_datasets()
        backfill_jobs = [normalise(j) for j in filter_jobs(backfill_raw)] if backfill_raw else []
        print(f"  [Apify] {len(backfill_jobs)} backfill jobs after filtering.")
    else:
        backfill_jobs = []

    csv_jobs = load_csv_jobs()
    # When each manual / supplemental job was first seen (their rows are rebuilt every run, so keep the original date)
    first_seen = {_job_key(j): j["date_added"] for j in csv_jobs if j["source"] in NON_APIFY_SOURCES and j["date_added"]}
    # Drop jobs older than JOB_MAX_AGE_DAYS based on date_posted
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=JOB_MAX_AGE_DAYS)).date()
    before = len(csv_jobs)
    def _age_date(j: dict) -> str:
        return (j["date_posted"] or j["date_added"] or "")[:10]
    csv_jobs = [j for j in csv_jobs if _age_date(j) and datetime.strptime(_age_date(j), "%Y-%m-%d").date() >= cutoff]
    print(f"  Dropped {before - len(csv_jobs)} jobs older than {JOB_MAX_AGE_DAYS} days from CSV.")
    # Strip out old Manual/supplemental entries from CSV — they'll be re-fetched fresh
    csv_apify_jobs = [j for j in csv_jobs if j["source"] not in NON_APIFY_SOURCES]

    # Merge: recent + backfill take precedence over CSV (placed first for dedup)
    apify_jobs = _dedup(recent_jobs + backfill_jobs + csv_apify_jobs)
    print(f"  [Apify] {len(apify_jobs)} total after merging with historical CSV.")

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

    # Drop any non-Manual job where the age date is older than JOB_MAX_AGE_DAYS
    before_cutoff = len(all_jobs)
    def _passes_cutoff(j: dict) -> bool:
        if j.get("source") == "Manual":
            return True
        age = (j.get("date_posted") or j.get("date_added") or "")[:10]
        if not age:
            return True
        try:
            return datetime.strptime(age, "%Y-%m-%d").date() >= cutoff
        except ValueError:
            return True
    all_jobs = [j for j in all_jobs if _passes_cutoff(j)]
    all_jobs = check_links(all_jobs)
    print(f"\n  Total unique jobs: {len(all_jobs)} ({before_cutoff - len(all_jobs)} dropped by age cutoff)")

    # ── 4. Apply hidden jobs filter for public dashboard ──────────
    # Hiding is a status, not a deletion: hidden jobs stay in the CSV and the admin page, and
    # come back to the public board when their URL is removed from hidden_jobs.txt.
    hidden = load_hidden_jobs()
    # Manual and supplemental jobs have no reliable posting date, so they expire JOB_MAX_AGE_DAYS after
    # they were first seen. Expired rows stay in the CSV (so the clock isn't reset while the source still
    # lists them) but are left off the public board and the admin page.
    today = datetime.now(timezone.utc).date()
    for j in all_jobs:
        if j["source"] in NON_APIFY_SOURCES:
            j["date_added"] = first_seen.get(_job_key(j), j["date_added"])
    def _expired(j):
        if j["source"] not in NON_APIFY_SOURCES:
            return False
        try:
            return (today - datetime.strptime(j["date_added"][:10], "%Y-%m-%d").date()).days > JOB_MAX_AGE_DAYS
        except ValueError:
            return False
    for j in all_jobs:
        j["status"] = "expired" if _expired(j) else ("hidden" if _url_key(j["url"]) in hidden else "active")
    n_exp = sum(j["status"] == "expired" for j in all_jobs)
    if n_exp:
        print(f"  {n_exp} manual/supplemental job(s) expired (first seen more than {JOB_MAX_AGE_DAYS} days ago).")
    n_hidden = sum(j["status"] == "hidden" for j in all_jobs)
    if n_hidden:
        print(f"  {n_hidden} job(s) marked hidden (kept in CSV and admin, not on the public dashboard).")

    # ── 5. Output ─────────────────────────────────────────────────
    all_jobs = [enrich(j) for j in all_jobs]
    public_jobs = [j for j in all_jobs if j["status"] == "active"]

    # Shrink guard: a run that would cut the board sharply is almost always a broken input, not real news.
    prev = _previous_active_count()
    if prev >= 100 and len(public_jobs) < prev * (1 - SHRINK_LIMIT) and not os.environ.get("ALLOW_SHRINK"):
        print(f"\nSTOPPED: the board would drop from {prev} to {len(public_jobs)} jobs "
              f"(more than {SHRINK_LIMIT:.0%}). Nothing was written. If this is intended, re-run with ALLOW_SHRINK=1.")
        sys.exit(1)

    save_csv(all_jobs)
    generate_html(public_jobs)           # public dashboard (no source, no delete)
    generate_admin_html([j for j in all_jobs if j["status"] != "expired"])   # admin dashboard (source + hide buttons)

    # Summary by source
    from collections import Counter
    sources = Counter(j["source"] for j in public_jobs)
    print("\n  Jobs by source:")
    for src, n in sorted(sources.items(), key=lambda x: x[0] or ""):
        print(f"    {(src or 'Unknown'):30s} {n}")

    print("\nDone!")
    print(f"  CSV       : {CSV_PATH}")
    print(f"  Dashboard : {HTML_PATH}")
    print(f"  Admin     : {ADMIN_PATH}")
    print("=" * 60)

    if RUN_ERRORS:
        print("\nRUN FAILED — Apify problems this run:")
        for e in RUN_ERRORS:
            print(f"  - {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
