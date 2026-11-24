"""Read-only audit: which Apify task(s) and schedule(s) cover each firm in data/orgs.csv.
Writes data/task_map.csv (firm -> tasks, schedules, last run, state). Touches nothing in Apify.
Run: python audit_apify_tasks.py   (needs APIFY_TOKEN). The Actions workflow 'Audit Apify tasks' runs it daily."""
import csv, os, pathlib, sys
from sync_apify_tasks import Apify, _items

DATA = pathlib.Path(__file__).parent / "data"
OUT = DATA / "task_map.csv"
COLS = ["org_name", "tasks", "schedules", "schedule_windows", "last_run", "last_run_status", "state"]
CHECK = DATA / "schedule_check.txt"


def load_firms():
    firms = {}
    with open(DATA / "orgs.csv", newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            names = {r["org_name"], r.get("search_term", ""), r.get("domain", "")} | set((r.get("aliases") or "").split("|"))   # a task that searches by website maps to the firm too
            firms[r["org_name"]] = {n.strip().lower() for n in names if n.strip()}
    return firms


def task_terms(inp: dict) -> set:
    return {str(x).strip().lower() for x in (inp.get("organizationSearch") or []) + (inp.get("domainFilter") or [])}


def audit(api) -> list[dict]:
    firms = load_firms()
    tasks = _items(api.call("GET", "/actor-tasks?limit=1000"))
    sched = _items(api.call("GET", "/schedules?limit=1000"))
    by_task = {}                                   # task id -> enabled schedule names
    for s in sched:
        if not s.get("isEnabled", True):
            continue
        for a in s.get("actions", []):
            if a.get("actorTaskId"):
                by_task.setdefault(a["actorTaskId"], []).append(s["name"])
    cover = {n: {"tasks": [], "schedules": [], "windows": [], "last": "", "status": ""} for n in firms}
    sched_info = {}                                # schedule name -> {"task", "window"}
    for t in tasks:
        inp = api.call("GET", f"/actor-tasks/{t['id']}/input")
        inp = inp.get("data", inp) if isinstance(inp, dict) else inp
        terms = task_terms(inp or {})
        window = (inp or {}).get("timeRange", "?")
        for sn in by_task.get(t["id"], []):
            sched_info[sn] = {"task": t["name"], "window": window}
        hit = [n for n, names in firms.items() if names & terms]
        if not hit:
            continue
        runs = _items(api.call("GET", f"/actor-tasks/{t['id']}/runs?limit=1&desc=1"))
        last = (runs[0].get("finishedAt") or runs[0].get("startedAt") or "")[:10] if runs else ""
        status = runs[0].get("status", "") if runs else ""
        for n in hit:
            c = cover[n]
            c["tasks"].append(t["name"]); c["schedules"] += by_task.get(t["id"], [])
            c["windows"] += [f"{sn}={window}" for sn in by_task.get(t["id"], [])]
            if last > c["last"]:
                c["last"], c["status"] = last, status
    rows = []
    for n, c in cover.items():
        state = "scheduled" if c["schedules"] else ("task, not scheduled" if c["tasks"] else "no task")
        rows.append({"org_name": n, "tasks": "; ".join(sorted(set(c["tasks"]))), "schedules": "; ".join(sorted(set(c["schedules"]))), "schedule_windows": "; ".join(sorted(set(c["windows"]))),
                     "last_run": c["last"], "last_run_status": c["status"], "state": state})
    audit.sched_info = sched_info
    return rows


def checks(rows, sched_info) -> list[str]:
    """Plain-text report: (1) scheduled tasks that do not look back 24h, (2) firms on more than one schedule."""
    out = ["1) SCHEDULES THAT DO NOT LOOK BACK 24 HOURS (a scheduled task should never pull 6 months)"]
    bad = {n: i for n, i in sched_info.items() if i["window"] != "24h"}
    out += [f"   {n}  ->  task {i['task']}  window {i['window']}" for n, i in sorted(bad.items())] or ["   none"]
    out += ["", "2) FIRMS ON MORE THAN ONE SCHEDULE"]
    multi = [r for r in rows if len([x for x in r["schedules"].split("; ") if x]) > 1]
    out += [f"   {r['org_name']}: {r['schedules']}" for r in sorted(multi, key=lambda r: r["org_name"].lower())] or ["   none"]
    return out


def write(rows, path=OUT):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, COLS); w.writeheader(); w.writerows(rows)


if __name__ == "__main__":
    tok = os.environ.get("APIFY_TOKEN") or sys.exit("STOP: APIFY_TOKEN not set")
    rows = audit(Apify(tok)); write(rows)
    CHECK.write_text("\n".join(checks(rows, audit.sched_info)) + "\n", encoding="utf-8")
    print(CHECK.read_text())
    from collections import Counter
    print(f"Wrote {OUT} ({len(rows)} firms):", dict(Counter(r["state"] for r in rows)))
