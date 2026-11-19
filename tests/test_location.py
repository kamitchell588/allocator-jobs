"""Location (metro) filter, seniority label and sector options. Run: python tests/test_location.py"""
import csv, importlib.util, pathlib, sys, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from run_pipeline import REPO, run
csv.field_size_limit(10**9)
spec = importlib.util.spec_from_file_location("sc", REPO / "scraper.py"); sc = importlib.util.module_from_spec(spec); spec.loader.exec_module(sc)
fails = []
def check(n, c):
    print(("PASS " if c else "FAIL ") + n)
    if not c: fails.append(n)
m = sc.metro_label
cases = {"New York, New York, United States": "New York", "Cambridge, Massachusetts, United States": "Boston",
         "Greenwich, Connecticut, United States": "Fairfield County, CT", "Austin, TX": "Austin", "New York, NY": "New York",
         "Washington, District of Columbia, United States": "Washington, DC", "Arlington County, Virginia, United States": "Washington, DC",
         "Arlington, Texas, United States": "Dallas–Fort Worth", "Toronto, Ontario, Canada": "Toronto", "Montreal, Quebec, Canada": "Montreal",
         "Stamford, New York, United States": "Other US", "New Jersey, United States": "Other US", "United States": "Other US",
         "Quinte West, Ontario, Canada": "Other Canada", "N/A": "Remote / Unknown", "": "Remote / Unknown", "Remote": "Remote / Unknown",
         "Short Hills, New Jersey, United States": "New York", "Linglestown, Pennsylvania, United States": "Other US"}
for loc, want in cases.items():
    check(f"{loc or '(blank)'} -> {want}", m(loc) == want)
check("seniority level 3 is 'Senior Associate / Manager'", sc.SENIORITY_LEVELS[2] == "Senior Associate / Manager")
check("a 'Senior Analyst' title lands in level 3", sc.seniority_label("Senior Investment Analyst") == "Senior Associate / Manager")
# real data
d = tempfile.mkdtemp(); run(d)
rows = list(csv.DictReader(open(pathlib.Path(d) / "allocator_jobs.csv", encoding="utf-8", newline="")))
from collections import Counter
c = Counter(r["metro"] for r in rows)
print("   ", dict(c.most_common()))
check("every job has a metro", all(r["metro"] for r in rows))
other = Counter(r["location"] for r in rows if r["metro"] == "Other US")
print("    Other US locations:", dict(other.most_common(12)))
html = open(pathlib.Path(d) / "dashboard.html", encoding="utf-8").read()
check("dashboard has a Location filter and data-metro cells", 'label: "Location"' in html and 'data-metro="New York"' in html)
check("old Region filter is gone", 'label: "Region"' not in html)
check("sector filter always lists Infrastructure and Secondaries", '"Infrastructure", "Secondaries"' in html)
check("seniority filter uses the new label", '"Senior Associate / Manager"' in html and '"Senior / Manager"' not in html)
print("\n%d failure(s)" % len(fails)); sys.exit(1 if fails else 0)
