"""Apify task generator, tested against a fake Apify (no network). Run: python tests/test_sync.py"""
import copy, pathlib, sys
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import sync_apify_tasks as s
fails = []
def check(n, c):
    print(("PASS " if c else "FAIL ") + n)
    if not c: fails.append(n)

class Fake:
    """Just enough of Apify's API: tasks, task inputs, schedules. Records every write."""
    def __init__(self, tasks=None):
        self.tasks, self.inputs, self.scheds, self.writes, self.n = {}, {}, {}, [], 0
        for name in (tasks or []):
            self._add(name, {"old": True})
    def _add(self, name, inp):
        self.n += 1; tid = f"t{self.n}"; self.tasks[tid] = {"id": tid, "name": name}; self.inputs[tid] = inp; return tid
    def call(self, method, path, body=None):
        if method == "GET" and path.startswith("/actor-tasks?"): return {"data": {"items": list(self.tasks.values())}}
        if method == "GET" and path.startswith("/schedules?"): return {"data": {"items": list(self.scheds.values())}}
        if method == "GET" and path.endswith("/input"): return self.inputs[path.split("/")[2]]
        self.writes.append((method, path))
        if method == "POST" and path == "/actor-tasks": return {"data": {"id": self._add(body["name"], body["input"])}}
        if method == "PUT" and path.endswith("/input"): self.inputs[path.split("/")[2]] = body; return body
        if method == "POST" and path == "/schedules": self.n += 1; self.scheds[body["name"]] = {"id": f"s{self.n}", **body}; return {"data": {}}
        raise AssertionError((method, path))

tasks = s.build_tasks()
firms = [f for t in tasks for f in t["firms"]]
import csv
allrows = list(csv.DictReader(open(ROOT / "data" / "orgs.csv", encoding="utf-8", newline="")))
want = [r for r in allrows if r["active"] == "Y" and r["priority"] == "P1" and not r["old_task"]]
check("every active P1 firm not in an old task lands in exactly one task", len(firms) == len(want) and len({f["org_name"] for f in firms}) == len(want))
check("no inactive firm is searched", not any(f["active"] != "Y" for f in firms))
check("only P1 firms by default", all(f["priority"] == "P1" for f in firms))
check("firms already in an older task are NOT in the generated tasks", not any(f["old_task"] for f in firms))
check("--priority P1,P2 adds P2 firms", len(s.build_tasks(("P1", "P2"))[0]["firms"]) >= 1 and sum(len(t["firms"]) for t in s.build_tasks(("P1", "P2"))) >= len(firms))
check("firms listed in the older tasks (old_task filled) number 199", sum(1 for r in allrows if r["old_task"]) == 199)
check("Citadel (inactive) is not searched", not any("Citadel" == t for tk in tasks for t in tk["input"].get("organizationSearch", [])))
check("no task has more than 40 firms", all(len(t["firms"]) <= s.MAX_FIRMS_PER_TASK for t in tasks))
check("task names are unique and start with fram-", len({t["name"] for t in tasks}) == len(tasks) and all(t["name"].startswith("fram-") for t in tasks))
from collections import defaultdict
_g = defaultdict(list)
for t in tasks: _g[t["name"].rsplit("-", 1)[0]].append(len(t["firms"]))
check("tasks of the same group are split evenly (sizes differ by at most 1)", all(max(v) - min(v) <= 1 for v in _g.values()))
check("every sector maps to a known group (nothing falls to 'other')", not any(t["name"].startswith("fram-other") for t in tasks))
t0 = tasks[0]["input"]
check("input carries the exclusion files (titles, companies)", len(t0["titleExclusionSearch"]) > 100 and "citadel credit union" in t0["organizationExclusionSearch"])
check("whole-word markers are not sent to Apify", not any(p.startswith("=") for p in t0["titleExclusionSearch"]) and "hr" in [p.lower() for p in t0["titleExclusionSearch"]])
check("24 hour window and a capped limit on every task", all(t["input"]["timeRange"] == "24h" and 100 <= t["input"]["limit"] <= 1000 for t in tasks))
check("input has the same switches as the hand-built tasks", all(k in t0 for k in ("includeAi", "descriptionType", "includeLinkedIn", "removeAgency", "remote only (legacy)")))

api = Fake(tasks=["my-own-task", "task-6"])
dry = s.sync(api, tasks, apply=False)
check("dry run writes nothing", api.writes == [] and len(dry["created"]) == len(tasks))
res = s.sync(api, tasks, apply=True)
check("apply creates every task but no schedule yet", len(res["created"]) == len(tasks) and len(api.scheds) == 0)
sch = s.sync(api, tasks, apply=True, schedule=True)
check("--schedule adds a daily schedule per task", len(api.scheds) == len(tasks))
check("your own tasks are untouched", api.inputs["t1"] == {"old": True} and api.tasks["t2"]["name"] == "task-6" and not any("t1" in w[1] or "t2" in w[1] for w in api.writes))
before = len(api.writes)
again = s.sync(api, tasks, apply=True)
check("a second apply changes nothing", len(api.writes) == before and len(again["unchanged"]) == len(tasks) and not again["created"] and not again["updated"])
# edit the master list: add a firm -> only its task updates
tasks2 = copy.deepcopy(tasks); tasks2[0]["input"]["organizationSearch"].append("New Firm LLC")
upd = s.sync(api, tasks2, apply=True)
check("changing one task updates only that task", upd["updated"] == [tasks2[0]["name"]] and len(upd["unchanged"]) == len(tasks) - 1)
gone = s.sync(api, tasks[1:], apply=True)
check("a task no longer needed is reported stale, not deleted", gone["stale"] == [tasks[0]["name"]] and tasks[0]["name"] in {t["name"] for t in api.tasks.values()})
# test one batch at a time
one = Fake(); only = [tasks[0]]
r1 = s.sync(one, only, apply=True, all_names=[t["name"] for t in tasks])
check("--only creates just the chosen task, no schedule, nothing marked stale", r1["created"] == [tasks[0]["name"]] and len(one.tasks) == 1 and not one.scheds and r1["stale"] == [])
r2 = s.sync(one, only, apply=True, schedule=True, all_names=[t["name"] for t in tasks])
check("later --schedule on that task adds only its schedule", len(one.scheds) == 1 and r2["unchanged"] == [tasks[0]["name"]])
print("\n%d failure(s)" % len(fails)); sys.exit(1 if fails else 0)
