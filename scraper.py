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


def _phrase_regex(phrases: list[str]):
    """One regex for a phrase list. A plain phrase matches anywhere in the title; a phrase written
    with a leading '=' (e.g. '=hr') matches only as a whole word, for short terms that would
    otherwise hit the middle of other words."""
    plain = [_re.escape(p) for p in phrases if not p.startswith("=")]
    whole = [_re.escape(p[1:].strip()) for p in phrases if p.startswith("=")]
    parts = plain + ([r"\b(?:" + "|".join(whole) + r")\b"] if whole else [])
    return _re.compile("|".join(parts))


def _load_optional_phrases(filename: str) -> list[str]:
    """Like _load_phrase_list but the file may be missing or empty (a blocklist has nothing by default)."""
    path = pathlib.Path(__file__).parent / "data" / filename
    try:
        return [l.strip().lower() for l in path.read_text(encoding="utf-8").splitlines()
                if l.strip() and not l.strip().startswith("#")]
    except OSError:
        return []



# ── Role-type keywords (must appear in job title): edit data/title_include.txt ──
LAYER2_TITLE_KEYWORDS = _load_phrase_list("title_include.txt")
_INCLUDE_RE = _phrase_regex(LAYER2_TITLE_KEYWORDS)

# ── Exclusion keywords: edit data/title_exclude.txt (no code change needed) ──
EXCLUDE_TITLE_KEYWORDS = _load_phrase_list("title_exclude.txt")
_EXCLUDE_RE = _phrase_regex(EXCLUDE_TITLE_KEYWORDS)

# ── Companies never to accept (look-alike names): edit data/exclude_companies.txt ──
EXCLUDE_COMPANIES = _load_optional_phrases("exclude_companies.txt")


def _company_excluded(name: str) -> bool:
    name = (name or "").lower()
    return any(p in name for p in EXCLUDE_COMPANIES)

CSV_COLUMNS = ["title", "company", "location", "date_posted", "url", "description", "job_type", "source", "date_added",
               "job_country", "job_state", "seniority",
               "org_priority", "org_sector", "org_region", "org_hq", "org_size_b", "experience_years", "status", "metro"]
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


_MATCHERS: dict = {}


def _match_org(table: dict, company: str):
    """Look up a company in a search_term-keyed table: exact match first, then the longest term that
    appears in the company name as whole words ('Careers at KKR' -> 'kkr', 'Berkshire Group, LLC' ->
    'berkshire group'). With locked domains this also catches look-alikes such as 'Weber Blackstone'.
    One compiled pattern per table (cached), so a large firm list stays fast."""
    c = (company or "").strip().lower()
    if c in table:
        return table[c]
    cached = _MATCHERS.get(id(table))
    if cached is None or cached[0] is not table:
        terms = sorted(table, key=len, reverse=True)
        rx = _re.compile(r"(?=(?<![a-z0-9])(" + "|".join(_re.escape(x) for x in terms) + r")(?![a-z0-9]))") if terms else None
        cached = (table, rx, {})
        _MATCHERS[id(table)] = cached
    _, rx, memo = cached
    if rx is None:
        return None
    if c not in memo:
        hits = [m.group(1) for m in rx.finditer(c)]
        memo[c] = table[max(hits, key=len)] if hits else None
    return memo[c]


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
                    for a in (r.get("aliases") or "").split("|"):
                        if a.strip():
                            _ORG_META.setdefault(a.strip().lower(), r)
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


# ── Scoped firms: only one division wanted (data/scoped_orgs.csv) ──────────────
_SCOPES = None


def _load_scopes() -> list[dict]:
    global _SCOPES
    if _SCOPES is None:
        _SCOPES = []
        path = pathlib.Path(__file__).parent / "data" / "scoped_orgs.csv"
        try:
            lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip() and not l.lstrip().startswith("#")]
        except OSError:
            lines = []
        for r in csv.DictReader(lines):
            name = (r.get("company") or "").strip().lower()
            if not name:
                continue
            split = lambda v: [x.strip().lower() for x in (v or "").split("|") if x.strip()]
            try:
                cap = int(r.get("cap") or 0)
            except ValueError:
                cap = 0
            _SCOPES.append({"name": name,
                            # company may list several spellings separated by |
                            "name_re": _re.compile(r"(?<![a-z0-9])(?:" + "|".join(_re.escape(x.strip()) for x in name.split("|") if x.strip()) + r")(?![a-z0-9])"),
                            "require": _phrase_regex(split(r.get("require_any"))) if split(r.get("require_any")) else None,
                            "and_any": _phrase_regex(split(r.get("and_any"))) if split(r.get("and_any")) else None,
                            "exclude": _phrase_regex(split(r.get("exclude_any"))) if split(r.get("exclude_any")) else None,
                            "cap": cap})
    return _SCOPES


def scope_for(company: str):
    c = (company or "").strip().lower()
    for sc in _load_scopes():
        if sc["name_re"].search(c):
            return sc
    return None


def scope_title_ok(sc: dict, title: str) -> bool:
    t = (title or "").lower()
    if sc["require"] and not sc["require"].search(t):
        return False
    if sc.get("and_any") and not sc["and_any"].search(t):      # optional second group that must also match
        return False
    if sc["exclude"] and sc["exclude"].search(t):
        return False
    # the shared exclusion list (data/title_exclude.txt) still applies, except that a firm's own division name
    # ("Asset & Wealth Management") must not trip its 'wealth management' phrase
    t = _re.sub(r"asset\s*(?:&|and)\s*wealth management", " ", t)
    return not _EXCLUDE_RE.search(t)


def apply_scope_caps(jobs: list[dict]) -> list[dict]:
    """Keep each scoped firm's newest `cap` jobs (cap 0 = no cap). Other firms are untouched."""
    kept, counts = [], {}
    for j in sorted(jobs, key=lambda j: j.get("date_posted") or "", reverse=True):
        sc = scope_for(j.get("company"))
        if sc and sc["cap"]:
            if counts.get(sc["name"], 0) >= sc["cap"]:
                continue
            counts[sc["name"]] = counts.get(sc["name"], 0) + 1
        kept.append(j)
    kept_ids = {id(j) for j in kept}
    return [j for j in jobs if id(j) in kept_ids]       # original order


def passes_layer1(item: dict) -> bool:
    """Layer 1: domain allowlist. If the job's company name matches a firm in
    data/orgs.csv that has a locked domain, the job's domain must equal it
    (blocks look-alike companies, e.g. wrong 'Citadel'). Other orgs pass."""
    org = str(item.get("organization") or "").strip().lower()
    dom = str(item.get("domain_derived") or "").strip().lower()
    if _company_excluded(org):
        return False
    expected = _match_org(_load_org_domains(), org)
    if expected:
        if not dom:                       # some job sources give no domain: accept only an exact firm-name match
            return org in _load_org_domains()
        return dom == expected or dom.endswith("." + expected)
    return True


def passes_layer2(item: dict) -> bool:
    """Layer 2: title must contain at least one relevant investment keyword."""
    title = _title_from_item(item)
    return bool(_INCLUDE_RE.search(title))


def passes_exclusions(item: dict) -> bool:
    """Return False if the title contains any exclusion keyword."""
    title = _title_from_item(item)
    return not _EXCLUDE_RE.search(title)


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
        sc = scope_for(item.get("organization") or item.get("company") or item.get("companyName"))
        if sc:                                   # scoped firm: its own keyword rules replace Layer 2 + exclusions
            if not scope_title_ok(sc, _title_from_item(item)):
                continue
            after_l2.append(item); after_ex.append(item)
            if passes_geo(item):
                after_geo.append(item)
            continue
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
SENIORITY_LEVELS = ["Intern / Entry", "Analyst / Associate", "Senior Associate / Manager",
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
    ("Secondaries", [r"\bcoller\b", r"landmark partners", r"lexington partners", r"harbourvest", r"pantheon", r"strategic partners"]),
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


# ── Location -> metro area (data/metros.csv) ────────────────────────────────
_US_STATES = {"alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar", "california": "ca", "colorado": "co",
    "connecticut": "ct", "delaware": "de", "district of columbia": "dc", "florida": "fl", "georgia": "ga", "hawaii": "hi",
    "idaho": "id", "illinois": "il", "indiana": "in", "iowa": "ia", "kansas": "ks", "kentucky": "ky", "louisiana": "la",
    "maine": "me", "maryland": "md", "massachusetts": "ma", "michigan": "mi", "minnesota": "mn", "mississippi": "ms",
    "missouri": "mo", "montana": "mt", "nebraska": "ne", "nevada": "nv", "new hampshire": "nh", "new jersey": "nj",
    "new mexico": "nm", "new york": "ny", "north carolina": "nc", "north dakota": "nd", "ohio": "oh", "oklahoma": "ok",
    "oregon": "or", "pennsylvania": "pa", "rhode island": "ri", "south carolina": "sc", "south dakota": "sd",
    "tennessee": "tn", "texas": "tx", "utah": "ut", "vermont": "vt", "virginia": "va", "washington": "wa",
    "west virginia": "wv", "wisconsin": "wi", "wyoming": "wy"}
_CA_PROVINCES = {"alberta": "ab", "british columbia": "bc", "manitoba": "mb", "new brunswick": "nb",
    "newfoundland and labrador": "nl", "nova scotia": "ns", "ontario": "on", "prince edward island": "pe",
    "quebec": "qc", "québec": "qc", "saskatchewan": "sk"}
_STATE_LOOKUP = {}
for _full, _ab in _US_STATES.items():
    _STATE_LOOKUP[_full] = (_full, "us"); _STATE_LOOKUP[_ab] = (_full, "us")
for _full, _ab in _CA_PROVINCES.items():
    _STATE_LOOKUP[_full] = (_full, "ca"); _STATE_LOOKUP[_ab] = (_full, "ca")
_METROS = None


def _load_metros() -> dict:
    """(city, state) lowercase -> metro name, from data/metros.csv."""
    global _METROS
    if _METROS is None:
        _METROS = {}
        path = pathlib.Path(__file__).parent / "data" / "metros.csv"
        try:
            with open(path, newline="", encoding="utf-8") as f:
                lines = [l for l in f if not l.startswith("#")]
            for r in csv.DictReader(lines):
                st = _STATE_LOOKUP.get((r.get("state") or "").strip().lower())
                if st and r.get("city") and r.get("metro"):
                    _METROS[(r["city"].strip().lower(), st[0])] = r["metro"].strip()
        except FileNotFoundError:
            pass
    return _METROS


def metro_label(location: str) -> str:
    """'Cambridge, Massachusetts, United States' -> 'Boston'. Unlisted places fall back to 'Other US' /
    'Other Canada'; no usable location -> 'Remote / Unknown'."""
    parts = [p.strip() for p in (location or "").split(",") if p.strip() and p.strip().lower() != "n/a"]
    country = ""
    if parts and parts[-1].lower() in ("united states", "usa", "us", "u.s.", "canada"):
        country = "ca" if parts.pop().lower() == "canada" else "us"
    state = ""
    if parts and parts[-1].lower() in _STATE_LOOKUP:
        state, st_country = _STATE_LOOKUP[parts.pop().lower()]
        country = country or st_country
    city = parts[-1].lower() if parts else ""
    if state and city and (city, state) in _load_metros():
        return _load_metros()[(city, state)]
    return {"us": "Other US", "ca": "Other Canada"}.get(country, "Remote / Unknown")


def enrich(j: dict) -> dict:
    """Fill seniority and sector labels (also repairs older CSV rows that lack them)."""
    cur = (j.get("seniority") or "").strip()
    if cur and cur[0].isdigit():            # older rows stored the raw Apify bucket here
        j["experience_years"], cur = cur, ""
    if cur not in SENIORITY_LEVELS and cur != "Not specified":
        j["seniority"] = seniority_label(j.get("title", ""), j.get("experience_years", ""))
    j["org_sector"] = sector_label(j)
    j["metro"] = metro_label(j.get("location", ""))
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
            f'<td data-metro="{escape(j.get("metro") or "")}">{escape(j["location"])}</td>'
            f'<td>{escape(j.get("seniority") or "")}</td>'
            f'<td>{escape(j.get("org_sector") or "")}</td>'
            f'<td data-sort="{escape(raw_date)}">{escape(raw_date)}</td>'
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
  /* ── FRAM Partners × iConnections design: black & white, serif headlines ── */
  :root {{
    --navy:      #14161f;
    --burgundy:  #000000;
    --cream:     #f7f5f1;
    --white:     #ffffff;
    --border:    #e4e4e7;
    --muted:     #6b6f80;
    --font-display: "Source Serif 4", Georgia, serif;
    --font-body:    "Inter", system-ui, sans-serif;
  }}

  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: var(--font-body); background: var(--white); color: var(--navy); min-height: 100vh; }}
  a {{ color: #000; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}

  /* ── Top bar ── */
  header {{ background: #000; color: #fff; padding: 1.1rem 2rem; }}
  .header-inner {{ max-width: 1400px; margin: 0 auto; display: flex; align-items: center; gap: 1.4rem; }}
  .header-inner .logo-fram {{ height: 32px; display: block; }}
  .header-inner .divider {{ width: 1px; height: 28px; background: #444; }}
  .header-inner .logo-ic {{ height: 26px; display: block; }}

  /* ── Hero ── */
  .hero {{ max-width: 1400px; margin: 0 auto; padding: 4rem 2rem 2.2rem; }}
  .hero .eyebrow {{ font-size: .72rem; font-weight: 600; letter-spacing: .14em; text-transform: uppercase; color: var(--muted); }}
  .hero h1 {{ font-family: var(--font-display); font-weight: 500; font-size: clamp(2rem, 4.6vw, 3.4rem);
              line-height: 1.07; letter-spacing: -.02em; margin: 1rem 0 .9rem; max-width: 800px; }}
  .hero p {{ color: var(--muted); font-size: 1.05rem; max-width: 640px; }}

  .container {{ max-width: 1400px; margin: 0 auto; padding: 0 2rem 4rem; }}

  /* ── Stat cards ── */
  .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1.25rem; margin-bottom: 2rem; }}
  .stat-card {{ background: var(--white); border: 1px solid var(--border); border-radius: 10px;
               padding: 1.5rem; box-shadow: 0 8px 24px rgba(20,22,31,.05); }}
  .stat-card .label {{ font-size: .7rem; text-transform: uppercase; letter-spacing: .14em; color: var(--muted); font-weight: 600; margin-bottom: .5rem; }}
  .stat-card .value {{ font-family: var(--font-display); font-size: 2.6rem; font-weight: 500; letter-spacing: -.02em; }}

  /* ── Top organizations ── */
  .section {{ background: var(--white); border: 1px solid var(--border); border-radius: 10px; padding: 1.5rem; margin-bottom: 2rem; }}
  .section h2 {{ font-family: var(--font-display); font-size: 1.15rem; font-weight: 500; margin-bottom: 1rem; }}
  .companies-wrap {{ display: flex; flex-wrap: wrap; gap: .5rem; }}
  .company-chip {{ display: flex; align-items: center; gap: .5rem; border: 1px solid #cfcfd6; border-radius: 999px;
                   padding: .3rem .5rem .3rem .9rem; font-size: .82rem; background: var(--white); }}
  .chip-name {{ font-weight: 500; }}
  .chip-count {{ background: #000; color: #fff; border-radius: 999px; padding: .1rem .55rem; font-size: .7rem; font-weight: 600; }}

  /* ── Table ── */
  .table-section {{ background: var(--white); border: 1px solid var(--border); border-radius: 10px; overflow: hidden; }}
  .table-toolbar {{ display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 1rem;
                    padding: 1.25rem 1.5rem; background: var(--white); }}
  .table-toolbar h2 {{ font-family: var(--font-display); font-size: 1.15rem; font-weight: 500; }}
  #searchBox {{ padding: .55rem 1rem; border: 1px solid #cfcfd6; border-radius: 999px; font-family: var(--font-body);
                font-size: .82rem; outline: none; width: 300px; max-width: 100%; background: var(--white); color: var(--navy); }}
  #searchBox:focus {{ border-color: #000; }}

  .table-wrap {{ overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; font-size: .85rem; }}
  th {{ background: var(--white); color: var(--muted); font-weight: 600; font-size: .68rem; text-transform: uppercase;
        letter-spacing: .14em; text-align: left; padding: .85rem 1rem; white-space: nowrap; cursor: pointer; user-select: none;
        border-bottom: 1.5px solid #000; }}
  th:hover {{ color: #000; }}
  th .sort-icon {{ margin-left: .3rem; opacity: .5; }}
  th.asc .sort-icon::after  {{ content: " ▲"; }}
  th.desc .sort-icon::after {{ content: " ▼"; }}
  td {{ padding: 1rem; border-bottom: 1px solid var(--border); vertical-align: top; }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: var(--cream); }}
  td:first-child a {{ font-family: var(--font-display); font-size: 1.05rem; font-weight: 500; color: var(--navy); }}
  td:first-child a:hover {{ text-decoration: underline; }}
  .desc-cell {{ color: var(--muted); font-size: .78rem; max-width: 320px; }}

  /* ── Recency: black and white only ── */
  tr.age-fresh td:nth-child(6) {{ color: #000; font-weight: 700; }}
  tr.age-recent td:nth-child(6) {{ color: var(--navy); }}
  tr.age-old td:nth-child(6) {{ color: var(--muted); }}
  tr.age-fresh  {{ border-left: 3px solid #000; }}
  tr.age-recent {{ border-left: 3px solid #9a9aa6; }}
  tr.age-old    {{ border-left: 3px solid var(--border); }}

  footer {{ text-align: center; color: var(--muted); font-size: .75rem; margin-top: 3rem; padding-top: 1.5rem;
            border-top: 1px solid var(--border); letter-spacing: .03em; }}

  @media (max-width: 640px) {{
    header {{ padding: 1rem; }}
    .hero {{ padding: 2.2rem 1rem 1.4rem; }}
    .container {{ padding: 0 1rem 2rem; }}
    #searchBox {{ width: 100%; }}
  }}

  #noResults {{ display: none; text-align: center; padding: 2rem; color: var(--muted); }}

  .source-tag {{ display: inline-block; padding: .15rem .6rem; font-size: .7rem; font-weight: 500; border: 1px solid var(--border);
                 border-radius: 999px; color: var(--muted); white-space: nowrap; }}
  .source-tag.manual {{ background: #000; border-color: #000; color: #fff; }}

  /* ── Region dropdown inside Location th ── */
  .loc-th {{ position: relative; }}
  .loc-th select {{ margin-left: .4rem; padding: .15rem .3rem; border: 1px solid #cfcfd6; border-radius: 4px;
                    font-size: .7rem; font-weight: 500; font-family: var(--font-body); color: var(--navy); background: #fff; cursor: pointer; }}

  /* ── Filters ── */
  .filter-bar {{ display: flex; gap: .6rem; flex-wrap: wrap; align-items: center; padding: 1rem 1.5rem;
                 border-top: 1px solid var(--border); border-bottom: 1px solid var(--border); background: var(--cream); }}
  .ms {{ position: relative; }}
  .ms-btn {{ padding: .5rem 1rem; border: 1px solid #cfcfd6; background: var(--white); color: var(--navy);
             font-family: var(--font-body); font-size: .82rem; font-weight: 500; cursor: pointer; border-radius: 999px; }}
  .new-star {{ color: #7c3aed; }}
  #newJobsBtn {{ border: 1.5px solid #7c3aed; font-weight: 700; }}
  #newJobsBtn.active {{ background: #7c3aed; border-color: #7c3aed; color: #fff; }}
  .ms-btn.active .new-star {{ color: #c4b5fd; }}
  .ms-btn.active {{ background: #000; color: #fff; border-color: #000; }}
  .ms-panel {{ display: none; position: absolute; z-index: 20; top: calc(100% + 6px); left: 0; min-width: 240px; max-width: 320px;
               background: var(--white); border: 1px solid var(--border); border-radius: 10px; box-shadow: 0 12px 32px rgba(20,22,31,.14); overflow: hidden; }}
  .ms.open .ms-panel {{ display: block; }}
  .ms-search {{ width: 100%; padding: .6rem .8rem; border: 0; border-bottom: 1px solid var(--border); font-size: .82rem; outline: none; font-family: var(--font-body); }}
  .ms-list {{ max-height: 260px; overflow-y: auto; padding: .3rem 0; }}
  .ms-opt {{ display: flex; gap: .5rem; align-items: center; padding: .35rem .9rem; font-size: .82rem; cursor: pointer; }}
  .ms-opt:hover {{ background: var(--cream); }}
  .ms-foot {{ padding: .5rem .9rem; border-top: 1px solid var(--border); font-size: .75rem; }}
  .ms-foot a {{ color: #000; cursor: pointer; text-decoration: underline; }}
  #clearAll {{ font-size: .78rem; color: #000; cursor: pointer; text-decoration: underline; background: none; border: 0; }}
</style>
</head>
<body>

<!-- ── Welcome gate: one click, no details collected ── -->
<div id="gate" style="position:fixed;inset:0;background:var(--navy);display:flex;align-items:center;justify-content:center;z-index:999;">
  <div style="background:#fff;padding:2.5rem 2rem;width:360px;max-width:calc(100vw - 2rem);text-align:center;">
    <img src="https://framcompany.com/assets/fram-logo-BYQ0yDsY.png" alt="FRAM Partners" style="height:30px;margin-bottom:1.5rem;display:block;margin-left:auto;margin-right:auto;" />
    <h2 style="font-family:var(--font-display);font-size:1.3rem;font-weight:400;color:var(--navy);margin-bottom:1.5rem;">Allocator Job Board</h2>
    <button id="gate-btn" style="width:100%;padding:.65rem;background:var(--navy);color:#fff;border:none;font-family:var(--font-body);font-size:.85rem;font-weight:500;cursor:pointer;letter-spacing:.04em;">View Jobs</button>
  </div>
</div>

<script>
// One click to enter; remembered on this browser so returning visitors skip it.
function openBoard() {{
  document.getElementById("gate").style.display = "none";
  try {{ localStorage.setItem("fram_gate_passed", "1"); }} catch (e) {{}}
}}
try {{ if (localStorage.getItem("fram_gate_passed") === "1") openBoard(); }} catch (e) {{}}
document.getElementById("gate-btn").addEventListener("click", openBoard);
</script>

<header>
  <div class="header-inner">
    <img class="logo-fram" src="https://raw.githubusercontent.com/kamitchell588/allocator-jobs/main/Fram_F_Logo_WhitePNG%20(1).png" alt="FRAM Partners" />
    <span class="divider"></span>
    <img class="logo-ic" src="assets/iconnections_logo_white.png" alt="iConnections" />
  </div>
</header>

<div class="hero">
  <div class="eyebrow">Allocator Job Board</div>
  <h1>Institutional Allocator Jobs</h1>
  <p>Endowments, pensions, family offices, sovereign wealth &amp; private markets — US &amp; Canada</p>
</div>

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
      <div style="display:flex;align-items:center;gap:.9rem;flex-wrap:wrap;">
        <h2>All Roles ({stats["total"]})</h2>
        <div class="ms" id="newJobs">
          <button type="button" class="ms-btn" id="newJobsBtn"><span class="new-star">&#9733;</span> New jobs &#9662;</button>
          <div class="ms-panel" style="min-width:190px;">
            <label class="ms-opt"><input type="radio" name="newJobsRange" value="7"> Past 7 days <span id="newCount7"></span></label>
            <label class="ms-opt"><input type="radio" name="newJobsRange" value="30"> Past 30 days <span id="newCount30"></span></label>
            <div class="ms-foot"><a id="newJobsClear">Clear</a></div>
          </div>
        </div>
      </div>
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
          </tr>
        </thead>
        <tbody id="tableBody">
          {rows if rows else '<tr><td colspan="6" style="text-align:center;padding:2rem;color:#94a3b8;">No allocator roles found in dataset.</td></tr>'}
        </tbody>
      </table>
      <div id="noResults">No results match your search.</div>
    </div>
  </div>

  <footer>
    <p style="margin-top:1.5rem;">Generated by allocator-jobs scraper &bull; {updated}</p>
    <p style="margin-top:.4rem;">
      <span style="color:#000;">&#9632;</span> Posted &le; 7 days &nbsp;
      <span style="color:#9a9aa6;">&#9632;</span> Posted 8–30 days &nbsp;
      <span style="color:#e3e5e8;">&#9632;</span> Older
    </p>
  </footer>

</div>

<script>
// ── Search + multi-select filters ──
const searchBox = document.getElementById("searchBox");
const tableBody = document.getElementById("tableBody");
const noResults = document.getElementById("noResults");
const filterBar = document.getElementById("filterBar");

const SENIORITY_ORDER = ["Intern / Entry","Analyst / Associate","Senior Associate / Manager","Director / VP / Principal","Executive / Head","Not specified"];

// Each filter: label, how to read a value list from a row, optional fixed option order
const FILTERS = [
  {{ key: "company",   label: "Company",   get: r => [r.cells[1].textContent.trim()] }},
  {{ key: "metro",     label: "Location",  get: r => [r.cells[2].dataset.metro || "Remote / Unknown"], byCount: true, last: ["Other US", "Other Canada", "Remote / Unknown"] }},
  {{ key: "seniority", label: "Seniority", get: r => [r.cells[3].textContent.trim() || "Not specified"], order: SENIORITY_ORDER }},
  {{ key: "sector",    label: "Sector",    get: r => [r.cells[4].textContent.trim() || "Not classified"], always: ["Infrastructure", "Secondaries"] }},
];
const selected = {{}};   // key -> Set of chosen values (empty = no restriction)

// ── "New jobs" filter: windows are measured from the visitor's date when the page loads,
//    so they stay current between daily rebuilds. Jobs with no readable posted date
//    never match 7 / 30 days (they only show with no range chosen). ──
let newRange = 0;   // 0 = all jobs, 7, or 30
function daysAgo(row) {{
  const d = (row.cells[5].dataset.sort || "").trim();
  const m = d.split("-").map(Number);
  if (m.length !== 3 || m.some(isNaN) || String(d).length !== 10) return null;
  const now = new Date(), today = Date.UTC(now.getFullYear(), now.getMonth(), now.getDate());
  return Math.round((today - Date.UTC(m[0], m[1] - 1, m[2])) / 86400000);
}}
function inRange(row, n) {{ const a = daysAgo(row); return a !== null && a >= -1 && a <= n; }}
function buildNewJobs() {{
  const wrap = document.getElementById("newJobs"), btn = document.getElementById("newJobsBtn");
  const rows = Array.from(tableBody.rows);
  [7, 30].forEach(n => {{ document.getElementById("newCount" + n).textContent = "(" + rows.filter(r => inRange(r, n)).length + ")"; }});
  function set(n) {{
    newRange = n;
    wrap.querySelectorAll("input").forEach(i => i.checked = (+i.value === n));
    btn.classList.toggle("active", n > 0);
    filterTable();
  }}
  wrap.querySelectorAll("input").forEach(i => i.addEventListener("change", () => set(+i.value)));
  document.getElementById("newJobsClear").addEventListener("click", () => set(0));
  btn.addEventListener("click", e => {{
    e.stopPropagation();
    document.querySelectorAll(".ms.open").forEach(m => {{ if (m !== wrap) m.classList.remove("open"); }});
    wrap.classList.toggle("open");
  }});
  wrap.querySelector(".ms-panel").addEventListener("click", e => e.stopPropagation());
  window.resetNewJobs = () => {{ newRange = 0; wrap.querySelectorAll("input").forEach(i => i.checked = false); btn.classList.remove("active"); }};
}}

function buildFilters() {{
  const rows = Array.from(tableBody.rows);
  FILTERS.forEach(f => {{
    selected[f.key] = new Set();
    const counts = {{}};
    rows.forEach(r => {{ f.get(r).forEach(v => {{ if (v) counts[v] = (counts[v] || 0) + 1; }}); }});
    (f.always || []).forEach(v => {{ if (!(v in counts)) counts[v] = 0; }});
    let opts = Object.keys(counts);
    const lastIdx = o => (f.last || []).indexOf(o);
    opts = f.order ? opts.filter(o => f.order.includes(o)).sort((a, b) => f.order.indexOf(a) - f.order.indexOf(b))
         : f.byCount ? opts.sort((a, b) => (lastIdx(a) >= 0) - (lastIdx(b) >= 0) || lastIdx(a) - lastIdx(b) || counts[b] - counts[a] || a.localeCompare(b))
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
  all.addEventListener("click", () => {{ FILTERS.forEach(f => f._reset && f._reset()); window.resetNewJobs && window.resetNewJobs(); searchBox.value = ""; filterTable(); }});
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
    if (show && newRange && !inRange(row, newRange)) show = false;
    row.style.display = show ? "" : "none";
    if (show) visible++;
  }});
  noResults.style.display = visible === 0 ? "block" : "none";
}}

searchBox.addEventListener("input", filterTable);
buildFilters();
buildNewJobs();

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
    --navy:     #14161f;
    --burgundy: #000000;
    --cream:    #f7f5f1;
    --border:   #e4e4e7;
    --muted:    #6b6f80;
    --font-display: "Source Serif 4", Georgia, serif;
    --font-body:    "Inter", system-ui, sans-serif;
  }}
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: var(--font-body); background: #fff; color: var(--navy); min-height: 100vh; }}
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
  header {{ background: #000; color: #fff; padding: 1.1rem 2rem; }}
  .header-inner {{ max-width: 1300px; margin: 0 auto; display: flex; align-items: center; gap: 1.4rem; }}
  .header-inner .logo-fram {{ height: 32px; display: block; }}
  .header-inner .divider {{ width: 1px; height: 28px; background: #444; }}
  .header-inner .logo-ic {{ height: 26px; display: block; }}
  .hero {{ max-width: 1300px; margin: 0 auto; padding: 2.6rem 2rem 1rem; }}
  .hero .eyebrow {{ font-size: .72rem; font-weight: 600; letter-spacing: .14em; text-transform: uppercase; color: var(--muted); }}
  .hero h1 {{ font-family: var(--font-display); font-size: 2.4rem; font-weight: 500; letter-spacing: -.02em; margin-top: .7rem; }}
  .hero .sub {{ font-size: .85rem; color: var(--muted); margin-top: .5rem; }}
  .admin-badge {{
    display: inline-block; border: 1.5px solid #000; color: #000;
    font-size: .65rem; font-weight: 700; letter-spacing: .12em;
    padding: .15rem .5rem; margin-left: .8rem; vertical-align: middle; border-radius: 4px;
  }}

  /* Main */
  main {{ max-width: 1300px; margin: 0 auto; padding: 1.5rem 2rem; }}

  /* Notice */
  .notice {{
    background: var(--cream); border: 1px solid var(--border); border-left: 3px solid #000; border-radius: 6px;
    padding: .8rem 1rem; font-size: .82rem; margin-bottom: 1.5rem; color: var(--navy);
  }}
  .notice code {{
    background: #fff; padding: .1rem .3rem; font-size: .8rem; font-family: monospace;
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
  table {{ width: 100%; border-collapse: collapse; background: #fff; font-size: .85rem; }}
  th {{ background: #fff; color: var(--muted); padding: .75rem .8rem; text-align: left; font-weight: 600; font-size: .68rem; text-transform: uppercase; letter-spacing: .14em; white-space: nowrap; border-bottom: 1.5px solid #000; }}
  td {{ padding: .55rem .8rem; border-bottom: 1px solid var(--border); vertical-align: top; }}
  tr:hover td {{ background: var(--cream); }}

  .source-tag {{
    display: inline-block; padding: .15rem .5rem;
    font-size: .68rem; font-weight: 500;
    background: #fff; border: 1px solid var(--border); border-radius: 999px;
    color: var(--muted); white-space: nowrap;
  }}

  /* Sortable headers */
  th[data-col] {{ user-select: none; }}
  th[data-col]:hover {{ color: #000; }}
  th[data-col].asc  .sort-icon::after {{ content: " ▲"; opacity: .7; }}
  th[data-col].desc .sort-icon::after {{ content: " ▼"; opacity: .7; }}
  th[data-col] .sort-icon {{ font-size: .65rem; }}

  /* Hide button */
  .hide-btn {{
    padding: .25rem .7rem; font-size: .75rem; font-family: var(--font-body);
    cursor: pointer; border: 1px solid #000; background: #fff; border-radius: 999px;
    color: #000; font-weight: 500; white-space: nowrap;
  }}
  .hide-btn:hover {{ background: var(--burgundy); color: #fff; }}

  /* Add Job by URL panel */
  .add-job-panel {{
    background: #fff; border: 1px solid var(--border); border-radius: 10px;
    padding: 1.2rem 1.4rem; margin-bottom: 1.5rem; box-shadow: 0 8px 24px rgba(20,22,31,.05);
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
  <div class="header-inner">
    <img class="logo-fram" src="https://raw.githubusercontent.com/kamitchell588/allocator-jobs/main/Fram_F_Logo_WhitePNG%20(1).png" alt="FRAM Partners" />
    <span class="divider"></span>
    <img class="logo-ic" src="assets/iconnections_logo_white.png" alt="iConnections" />
  </div>
</header>
<div class="hero">
  <div class="eyebrow">Internal</div>
  <h1>Allocator Jobs <span class="admin-badge">ADMIN</span></h1>
  <div class="sub">Last updated: {updated} &nbsp;·&nbsp; {len(jobs)} total jobs (including hidden)</div>
</div>

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
    n0 = len(csv_jobs)
    csv_jobs = [j for j in csv_jobs if j["source"] == "Manual" or not _company_excluded(j["company"])]
    if len(csv_jobs) < n0:
        print(f"  Removed {n0 - len(csv_jobs)} history job(s) from excluded companies (data/exclude_companies.txt).")
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
    # History is re-checked against the current title rules, so a new exclusion also clears old rows
    def _title_still_ok(j: dict) -> bool:
        sc = scope_for(j["company"])
        return scope_title_ok(sc, j["title"]) if sc else passes_exclusions({"title": j["title"]})
    n_h = len(csv_apify_jobs)
    csv_apify_jobs = [j for j in csv_apify_jobs if _title_still_ok(j)]
    if len(csv_apify_jobs) < n_h:
        print(f"  Removed {n_h - len(csv_apify_jobs)} history job(s) whose title now matches data/title_exclude.txt.")

    # Merge: recent + backfill take precedence over CSV (placed first for dedup)
    apify_jobs = _dedup(recent_jobs + backfill_jobs + csv_apify_jobs)
    n_sc = len(apify_jobs)
    apify_jobs = apply_scope_caps(apify_jobs)
    if len(apify_jobs) < n_sc:
        print(f"  Scoped-firm cap (data/scoped_orgs.csv) removed {n_sc - len(apify_jobs)} job(s).")
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
        if not _INCLUDE_RE.search(title):
            return False
        if _EXCLUDE_RE.search(title):
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
