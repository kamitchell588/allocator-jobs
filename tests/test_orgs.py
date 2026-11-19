"""Look-alike company names must not slip in. Run: python tests/test_orgs.py"""
import csv, importlib.util, pathlib, sys, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from run_pipeline import run, REPO
csv.field_size_limit(10**9)
spec = importlib.util.spec_from_file_location("sc", REPO / "scraper.py"); sc = importlib.util.module_from_spec(spec); spec.loader.exec_module(sc)
fails = []
def check(n, c):
    print(("PASS " if c else "FAIL ") + n)
    if not c: fails.append(n)
I = lambda org, dom: {"organization": org, "domain_derived": dom, "title": "Investment Analyst"}
check("Citadel Credit Union (citadelbanking.com) is rejected", not sc.passes_layer1(I("Citadel Credit Union", "citadelbanking.com")))
check("Citadel Federal Solutions (falcontek.com) is rejected", not sc.passes_layer1(I("Citadel Federal Solutions", "falcontek.com")))
check("The Citadel (citadel.edu) is rejected", not sc.passes_layer1(I("The Citadel", "citadel.edu")))
check("Citadel with citadel.com is accepted", sc.passes_layer1(I("Citadel", "citadel.com")))
check("Citadel Securities page on citadel.com is accepted", sc.passes_layer1(I("Citadel Securities", "citadel.com")))
d = tempfile.mkdtemp(); run(d, hidden_text=(REPO / "hidden_jobs.txt").read_text())
cos = {r["company"] for r in csv.DictReader(open(pathlib.Path(d) / "allocator_jobs.csv", encoding="utf-8", newline="")) if r["status"] == "active"}
check("no Citadel look-alikes in the board built from real data", not [c for c in cos if "citadel" in c.lower()])
print("\n%d failure(s)" % len(fails)); sys.exit(1 if fails else 0)
