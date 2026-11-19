"""Manual / supplemental jobs expire 60 days after first seen. Run: python tests/test_expiry.py"""
import csv, datetime, html, pathlib, sys, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from run_pipeline import run, FIX
csv.field_size_limit(10**9)
tmp = tempfile.mkdtemp
rows = lambda d: list(csv.DictReader(open(pathlib.Path(d) / "allocator_jobs.csv", encoding="utf-8", newline="")))
today = datetime.date.today()
day = lambda n: (today - datetime.timedelta(days=n)).isoformat()
_d = tmp(); run(_d); base = rows(_d)
cols = list(base[0].keys())
def job(url, title, src, added):
    return {c: "" for c in cols} | {"url": url, "title": title, "company": "Acme", "location": "New York, NY",
                                    "source": src, "date_added": added, "status": "active"}
def hist(rs):
    p = pathlib.Path(tmp()) / "h.csv"
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(rs)
    return p
fails = []
def check(n, c):
    print(("PASS " if c else "FAIL ") + n)
    if not c: fails.append(n)

# sheet still lists both jobs; the old one was first seen 70 days ago, the other 10 days ago
sheet = [job("http://m/old", "Associate Old", "Manual", day(0)), job("http://m/new", "Associate New", "Manual", day(0)),
         job("http://u/old", "Analyst Utimco Old", "UTIMCO", day(0))]
h = hist([job("http://m/old", "Associate Old", "Manual", day(70)), job("http://m/new", "Associate New", "Manual", day(10)),
          job("http://u/old", "Analyst Utimco Old", "UTIMCO", day(61))] + [job(f"http://x/{i}", f"A{i}", "Apify", day(1)) for i in range(5)])
d = tmp(); code, log = run(d, history_csv=h, items=[], supplemental=sheet, hidden_text="")
r = {x["url"]: x for x in rows(d)}
dash = open(pathlib.Path(d) / "dashboard.html", encoding="utf-8").read()
adm = open(pathlib.Path(d) / "admin.html", encoding="utf-8").read()
check("run succeeds", code == 0)
check("manual job first seen 70 days ago is expired", r["http://m/old"]["status"] == "expired")
check("supplemental job first seen 61 days ago is expired", r["http://u/old"]["status"] == "expired")
check("manual job first seen 10 days ago stays active", r["http://m/new"]["status"] == "active")
check("original first-seen date is kept (not reset to today)", r["http://m/old"]["date_added"] == day(70))
check("expired job is not on the public board or admin", "http://m/old" not in dash and "http://m/old" not in adm and "http://u/old" not in dash)
check("active manual job is on the public board", "http://m/new" in dash)
# a brand-new manual job starts its clock today
d2 = tmp(); run(d2, history_csv=hist([]), items=[], supplemental=[job("http://m/fresh", "Associate Fresh", "Manual", day(0))], hidden_text="", allow_shrink=True)
check("new manual job is active, date_added today", {x["url"]: x for x in rows(d2)}["http://m/fresh"]["status"] == "active")
# removed from the sheet -> disappears from the CSV entirely
d3 = tmp(); run(d3, history_csv=h, items=[], supplemental=[], hidden_text="", allow_shrink=True)
check("job removed from the sheet is dropped from the CSV", not any(x["source"] in ("Manual", "UTIMCO") for x in rows(d3)))
print("\n%d failure(s)" % len(fails)); sys.exit(1 if fails else 0)
