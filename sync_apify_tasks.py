"""Build the Apify tasks from the master files, so the tasks never drift from the repo.

  python sync_apify_tasks.py            dry run: prints what it would create/update, changes nothing
  python sync_apify_tasks.py --apply    creates/updates the tasks (no schedule yet: run them by hand first)
  --only fram-hedge-funds-1[,name2]     limit to these tasks (test one batch at a time)
  --priority P1,P2                      which priorities to include (default P1)
  --schedule                            also create the daily schedule for the selected tasks
  --backfill                            first-run versions: same firms, but 6 months of postings, never scheduled
                                        (named fram-backfill-<task>). Run each once by hand; the daily task stays 24h.

Reads:   data/orgs.csv, data/task_groups.csv, data/title_exclude.txt, data/exclude_companies.txt
Which firms: active=Y, priority P1 (change with --priority P1,P2), and not already in one of your older hand-built
         tasks (the old_task column is filled). To move a firm from an old task to a generated one, retire the old
         task's copy and clear old_task.
Touches: only Apify tasks (and schedules) whose name starts with 'fram-'. Your other tasks are never read or changed.
Needs:   APIFY_TOKEN in the environment or .env
"""
import json
import csv, os, pathlib, re, sys

ROOT = pathlib.Path(__file__).parent
ACTOR = "fantastic-jobs~career-site-job-listing-api"
PREFIX = "fram-"
MAX_FIRMS_PER_TASK = 40
JOBS_PER_FIRM_PER_DAY = 20      # job cap per task = firms x this (min 100): a ceiling on spend, not a target
MIN_LIMIT = 100
MAX_LIMIT = 1000
BACKFILL_JOBS_PER_FIRM = 50     # first-run cap per firm (6 months of postings); MAX_BACKFILL_LIMIT is the ceiling
MAX_BACKFILL_LIMIT = 2000
COST_PER_1000_JOBS = 4.0        # USD, from the actor's pay-per-job price: used only for the worst-case estimate
SCHEDULE_CRON = "0 11 * * *"    # 11:00 UTC daily, three hours before the 14:00 UTC scraper run
GROUP_ORDER = ["hedge-funds", "private-equity", "credit-real-assets", "institutions", "advisors", "other"]

BASE_INPUT = {                  # same settings as the hand-built tasks
    "aiHasSalary": False, "aiVisaSponsorshipFilter": False, "descriptionType": "text", "includeAi": True,
    "includeLinkedIn": False, "includeCompanyDetails": False, "hasSalary": False, "hasNoLocation": False,
    "populateAiRemoteLocation": False, "populateAiRemoteLocationDerived": False, "remote only (legacy)": False,
    "removeAgency": False, "timeRange": "24h",
}


def _read_lines(name):
    p = ROOT / "data" / name
    if not p.exists():
        return []
    return [l.strip() for l in p.read_text(encoding="utf-8").splitlines() if l.strip() and not l.strip().startswith("#")]


def load_groups() -> dict:
    lines = [l for l in (ROOT / "data" / "task_groups.csv").read_text(encoding="utf-8").splitlines() if not l.startswith("#")]
    return {r["sector"].strip(): r["group"].strip() for r in csv.DictReader(lines)}


def load_active_firms(priorities=("P1",)) -> list[dict]:
    """Firms the generated tasks should search: active, wanted priority, not already in an older task."""
    with open(ROOT / "data" / "orgs.csv", newline="", encoding="utf-8") as f:
        return [r for r in csv.DictReader(f)
                if (r.get("active") or "").strip().upper() == "Y" and (r.get("priority") or "") in priorities
                and not (r.get("old_task") or "").strip()]


def title_exclusions() -> list[str]:
    """Phrases for Apify's title filter. '=' (whole-word marker) is dropped: Apify matches words already."""
    seen, out = set(), []
    for l in _read_lines("title_exclude.txt"):
        p = l.lstrip("=").strip()
        if p and p.lower() not in seen:
            seen.add(p.lower()); out.append(p)
    return out


def load_custom_tasks() -> list[dict]:
    """Hand-designed tasks kept in data/custom_tasks.json (e.g. one firm's alternatives group only).
    Each: {name (must start fram-), firms: [names], schedule: true/false, input: {...overrides of BASE_INPUT}}."""
    p = ROOT / "data" / "custom_tasks.json"
    if not p.exists():
        return []
    out = []
    for c in json.loads(p.read_text(encoding="utf-8")):
        if not c["name"].startswith(PREFIX):
            raise SystemExit(f"custom task {c['name']!r} must start with {PREFIX!r}")
        inp = dict(BASE_INPUT)
        inp.update(c["input"])
        inp.setdefault("titleExclusionSearch", _read_lines("title_exclude.txt"))
        out.append({"name": c["name"], "input": inp, "firms": [{"org_name": n} for n in c.get("firms", [])],
                    "custom": True, "schedule_ok": bool(c.get("schedule", False))})
    return out


def build_tasks(priorities=("P1",)) -> list[dict]:
    """[{name, input, firms}] for every active firm, grouped by sector family and split at 40 firms."""
    groups = load_groups()
    by_group: dict[str, list[dict]] = {}
    exact = []                       # name-risk firms with a locked domain: searched by website, not by name
    for r in load_active_firms(priorities):
        if (r.get("name_risk") or "").strip().upper() == "Y" and (r.get("domain") or "").strip():
            exact.append(r)
        else:
            by_group.setdefault(groups.get(r["sector"].strip(), "other"), []).append(r)
    excl_titles, excl_orgs = title_exclusions(), _read_lines("exclude_companies.txt")
    tasks = []
    for g in sorted(by_group, key=lambda g: (GROUP_ORDER.index(g) if g in GROUP_ORDER else 99, g)):
        firms = sorted(by_group[g], key=lambda r: (r.get("priority") or "P9", r["org_name"].lower()))
        n_tasks = -(-len(firms) // MAX_FIRMS_PER_TASK)          # as few tasks as possible, split evenly
        base, extra = divmod(len(firms), n_tasks)                 # first `extra` tasks get one more firm
        start = 0
        for k in range(n_tasks):
            size = base + (1 if k < extra else 0)
            chunk, i = firms[start:start + size], k * size
            start += size
            terms, seen = [], set()
            for r in chunk:
                t = (r.get("search_term") or r["org_name"]).strip()
                if t.lower() not in seen:
                    seen.add(t.lower()); terms.append(t)
            inp = dict(BASE_INPUT)
            inp["limit"] = max(MIN_LIMIT, min(MAX_LIMIT, len(terms) * JOBS_PER_FIRM_PER_DAY))
            inp["organizationSearch"] = terms
            if excl_orgs:
                inp["organizationExclusionSearch"] = excl_orgs
            inp["titleExclusionSearch"] = excl_titles
            tasks.append({"name": f"{PREFIX}{g}-{k + 1}", "input": inp, "firms": chunk})
    if exact:
        exact.sort(key=lambda r: r["org_name"].lower())
        domains = sorted({r["domain"].strip().lower() for r in exact})
        inp = dict(BASE_INPUT)
        inp["limit"] = max(MIN_LIMIT, min(MAX_LIMIT, len(domains) * JOBS_PER_FIRM_PER_DAY))
        inp["domainFilter"] = domains            # exact website match: no look-alike companies
        inp["titleExclusionSearch"] = excl_titles
        tasks.append({"name": f"{PREFIX}exact-domain-1", "input": inp, "firms": exact})
    tasks += load_custom_tasks()
    return tasks


def backfill_tasks(tasks: list[dict]) -> list[dict]:
    """One-off 6-month twin of each task: same firms and filters, bigger cap, never scheduled."""
    out = []
    for t in [x for x in tasks if not x.get("custom")]:
        inp = dict(t["input"])
        n = len(inp.get("organizationSearch") or inp.get("domainFilter") or [])
        inp["timeRange"] = "6m"
        inp["limit"] = max(MIN_LIMIT, min(MAX_BACKFILL_LIMIT, n * BACKFILL_JOBS_PER_FIRM))
        out.append({"name": f"{PREFIX}backfill-{t['name'][len(PREFIX):]}", "input": inp, "firms": t["firms"], "backfill": True})
    return out


class Apify:
    """Minimal Apify API client (tests replace it with a fake)."""
    def __init__(self, token):
        import requests
        self.s, self.base = requests.Session(), "https://api.apify.com/v2"
        self.s.headers["Authorization"] = f"Bearer {token}"
    def call(self, method, path, body=None):
        r = self.s.request(method, self.base + path, json=body, timeout=60)
        if r.status_code >= 300:
            raise RuntimeError(f"Apify {method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.text else {}


def _items(resp):
    d = resp.get("data", resp)
    return d.get("items", d) if isinstance(d, dict) else d


def sync(api, tasks, apply=False, schedule=False, all_names=None, out=print) -> dict:
    existing = {t["name"]: t for t in _items(api.call("GET", "/actor-tasks?limit=1000")) if t["name"].startswith(PREFIX)}
    schedules = {s["name"]: s for s in _items(api.call("GET", "/schedules?limit=1000")) if s["name"].startswith(PREFIX)}
    result = {"created": [], "updated": [], "unchanged": [], "stale": [], "scheduled": []}
    for t in tasks:
        name, inp = t["name"], t["input"]
        if name in existing:
            tid = existing[name]["id"]
            cur = api.call("GET", f"/actor-tasks/{tid}/input")
            cur = cur.get("data", cur) if isinstance(cur, dict) else cur
            if cur == inp:
                result["unchanged"].append(name)
            else:
                result["updated"].append(name)
                if apply: api.call("PUT", f"/actor-tasks/{tid}/input", inp)
        else:
            result["created"].append(name)
            if apply:
                tid = api.call("POST", "/actor-tasks", {"actId": ACTOR, "name": name, "input": inp})["data"]["id"]
                existing[name] = {"id": tid, "name": name}
        sname = f"{PREFIX}daily-{name[len(PREFIX):]}"
        if schedule and not t.get("backfill") and t.get("schedule_ok", True) and sname not in schedules:
            result["scheduled"].append(sname)
            if apply and name in existing:
                api.call("POST", "/schedules", {"name": sname, "cronExpression": SCHEDULE_CRON, "isEnabled": True, "isExclusive": True,
                         "actions": [{"type": "RUN_ACTOR_TASK", "actorTaskId": existing[name]["id"]}]})
    wanted = set(all_names) if all_names else {t["name"] for t in tasks}
    result["stale"] = sorted(n for n in existing if n not in wanted)
    return result


def report(tasks, result, apply, out=print):
    out(f"{'APPLIED' if apply else 'DRY RUN (nothing changed)'}: {len(tasks)} task(s) from {sum(len(t['firms']) for t in tasks)} firms not yet in an older task")
    worst = 0.0
    for t in tasks:
        lim = t["input"]["limit"]; worst += lim * COST_PER_1000_JOBS / 1000
        out(f"  {t['name']:28s} {len(t['input'].get('organizationSearch') or t['input'].get('domainFilter') or []):3d} firms  limit {lim:4d}  title exclusions {len(t['input']['titleExclusionSearch'])}")
    out(f"  worst-case spend if every task hits its limit: about ${worst:.2f}/day (real spend is usually far lower)")
    for k in ("created", "updated", "unchanged", "scheduled", "stale"):
        if result[k]: out(f"  {k}: {', '.join(result[k])}")
    if result["stale"]: out("  (stale tasks are left alone: delete them in Apify if you no longer want them)")


def main(argv):
    apply = "--apply" in argv
    prio = tuple(x.strip() for x in argv[argv.index("--priority") + 1].split(",")) if "--priority" in argv else ("P1",)
    tasks = build_tasks(prio)
    if "--backfill" in argv:
        tasks = tasks + backfill_tasks(tasks)
    all_names = [t["name"] for t in tasks]
    if "--custom-only" in argv:            # just the hand-designed tasks in data/custom_tasks.json
        tasks = [t for t in tasks if t.get("custom")]
    if "--only" in argv:
        only = {n.strip() for n in argv[argv.index("--only") + 1].split(",") if n.strip()}
        unknown = only - set(all_names)
        if unknown:
            sys.exit(f"Unknown task(s): {', '.join(sorted(unknown))}. Known: {', '.join(all_names)}")
        tasks = [t for t in tasks if t["name"] in only]
    token = os.environ.get("APIFY_TOKEN", "")
    if not token and (ROOT / ".env").exists():
        m = re.search(r"^APIFY_TOKEN=(.+)$", (ROOT / ".env").read_text(), re.M)
        token = m.group(1).strip() if m else ""
    if not token:
        sys.exit("APIFY_TOKEN is not set")
    report(tasks, sync(Apify(token), tasks, apply=apply, schedule="--schedule" in argv, all_names=all_names), apply)


if __name__ == "__main__":
    main(sys.argv[1:])
