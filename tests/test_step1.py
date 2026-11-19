"""Behavior tests for step 1. Run: python tests/test_step1.py  (no network needed)."""
import csv, html, pathlib, sys, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from run_pipeline import run, FIX
csv.field_size_limit(10**9)
rows = lambda d: list(csv.DictReader(open(pathlib.Path(d) / "allocator_jobs.csv", encoding="utf-8", newline="")))
tmp = lambda: tempfile.mkdtemp()
fails = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond: fails.append(name)

# 1. hiding is reversible
d0 = tmp(); run(d0)
victim = rows(d0)[5]["url"]
d1 = tmp(); run(d1, hidden_text=victim + "\n")
r1 = rows(d1)
check("hidden job stays in CSV with status=hidden", any(r["url"] == victim and r["status"] == "hidden" for r in r1))
check("hidden job not on public dashboard", html.escape(victim) not in open(pathlib.Path(d1) / "dashboard.html", encoding="utf-8").read())
check("hidden job still on admin page", html.escape(victim) in open(pathlib.Path(d1) / "admin.html", encoding="utf-8").read())
# second run starts from the CSV that run 1 wrote, with the job un-hidden
d2 = tmp(); run(d2, hidden_text="", history_csv=pathlib.Path(d1) / "allocator_jobs.csv", items=[])
r2 = rows(d2)
check("unhiding brings the job back (status active, in public dashboard)",
      any(r["url"] == victim and r["status"] == "active" for r in r2)
      and html.escape(victim) in open(pathlib.Path(d2) / "dashboard.html", encoding="utf-8").read())

# 2. dedup on link
from run_pipeline import REPO
sys.path.insert(0, str(REPO))
import importlib.util
spec = importlib.util.spec_from_file_location("sc", REPO / "scraper.py"); sc = importlib.util.module_from_spec(spec); spec.loader.exec_module(sc)
J = lambda u, loc="NY": {"title": "Analyst", "company": "Acme", "location": loc, "url": u}
check("same link twice -> one job", len(sc._dedup([J("http://a/1"), J("http://a/1/")])) == 1)
check("same title, different links -> both kept", len(sc._dedup([J("http://a/1"), J("http://a/2", "LA")])) == 2)
check("no link: same title+company+location -> one job", len(sc._dedup([J(""), J("")])) == 1)
check("no link: same title, different location -> both", len(sc._dedup([J("", "NY"), J("", "LA")])) == 2)

# 3. empty include list stops the run
code, log = run(tmp(), include_text="# only comments\n")
check("empty title include list stops the run", code != 0 and "STOP" in log)

# 4. shrink guard
hist = pathlib.Path(tmp()) / "h.csv"
src = rows(tmp.__call__() if False else d0)
with open(hist, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=list(src[0].keys())); w.writeheader()
    for r in src: w.writerow(r)
d4 = tmp(); code, log = run(d4, history_csv=hist, items=[], allow_shrink=False, hidden_text="")
# no fresh items; history is the same size, so no shrink
check("no shrink -> run succeeds", code == 0)
# history with many extra recent rows the new run will not reproduce (simulate: shrink by adding 300 fake rows to history, then dropping them via age)
import datetime
old = (datetime.date.today() - datetime.timedelta(days=90)).isoformat()
big = pathlib.Path(tmp()) / "big.csv"
with open(big, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=list(src[0].keys())); w.writeheader()
    for i in range(400):
        w.writerow({**src[0], "url": f"http://x/{i}", "date_posted": datetime.date.today().isoformat(), "status": "active"})
d5 = tmp(); code, log = run(d5, history_csv=big, items=[], allow_shrink=False, hidden_text="" )
check("normal run keeps all 400", code == 0 and len(rows(d5)) == 400)
# now hide 300 of them -> public board drops 75%
d6 = tmp(); code, log = run(d6, history_csv=big, items=[], allow_shrink=False,
                            hidden_text="\n".join(f"http://x/{i}" for i in range(300)))
check("big drop is stopped: exit 1, CSV and dashboard untouched", code == 1 and "STOPPED" in log and all(r["status"] == "active" for r in rows(d6)) and not (pathlib.Path(d6) / "dashboard.html").exists())
d7 = tmp(); code, log = run(d7, history_csv=big, items=[], allow_shrink=True,
                            hidden_text="\n".join(f"http://x/{i}" for i in range(300)))
check("ALLOW_SHRINK=1 lets an intended drop through", code == 0 and len(rows(d7)) == 400)
print("\n%d failure(s)" % len(fails)); sys.exit(1 if fails else 0)
