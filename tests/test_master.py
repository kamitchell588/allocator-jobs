"""Master firm list and filter-file checks. Run: python tests/test_master.py"""
import csv, importlib.util, pathlib, re, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from run_pipeline import REPO
spec = importlib.util.spec_from_file_location("sc", REPO / "scraper.py"); sc = importlib.util.module_from_spec(spec); spec.loader.exec_module(sc)
fails = []
def check(n, c):
    print(("PASS " if c else "FAIL ") + n)
    if not c: fails.append(n)
rows = list(csv.DictReader(open(REPO / "data" / "orgs.csv", encoding="utf-8", newline="")))
norm = lambda s: re.sub(r"[^a-z0-9 ]", "", s.lower().replace("&", " and ")).strip()
names = [norm(r["org_name"]) for r in rows]
check("every firm name appears once", len(names) == len(set(names)))
check("every firm has a sector, region and search term", all(r["sector"] and r["region"] and r["search_term"] for r in rows))
check("active is Y or N on every row", all(r["active"] in ("Y", "N") for r in rows))
check("priority is P1/P2/P3 on every row", all(r["priority"] in ("P1", "P2", "P3") for r in rows))
terms = {}
for r in rows:
    for t in [r["search_term"]] + [a for a in r["aliases"].split("|") if a]:
        terms.setdefault(t.lower(), set()).add(r["org_name"])
# the same term on several rows is only fine for a parent with sub-brands (Blackstone, KKR, ...)
shared = {t: v for t, v in terms.items() if len(v) > 1}
check("shared search terms are limited to known parent firms", set(shared) <= {"blackstone", "kkr", "carlyle", "bain capital", "blue owl", "iconiq", "icg", "jfg advisors", "pathway capital"}, ) or print("   ", sorted(shared))
check("Citadel is locked to citadel.com and kept out of Apify tasks", any(r["org_name"] == "Citadel" and r["domain"] == "citadel.com" and r["active"] == "N" for r in rows))
# aliases and matching
meta = sc._load_org_meta()
check("alias 'BCI' finds British Columbia Investment Management", (sc._match_org(meta, "BCI") or {}).get("org_name", "").startswith("British Columbia"))
check("'Careers at KKR' still matches KKR", (sc._match_org(meta, "Careers at KKR") or {}).get("search_term") == "KKR")
t0 = time.time(); [sc._match_org(meta, f"Some Company {i} Holdings") for i in range(3000)]
check("matching 3,000 company names against the full list is fast (<2s)", time.time() - t0 < 2)
# whole-word phrases
ex = sc._EXCLUDE_RE
check("'=hr' excludes 'HR Business Partner'", bool(ex.search("hr business partner")))
check("'=hr' does not exclude 'Three Rivers Investment Analyst'", not ex.search("three rivers investment analyst"))
check("'=gas' does not exclude 'Las Vegas Portfolio Analyst'", not ex.search("las vegas portfolio analyst"))
check("plain phrase 'payroll' excludes 'Payroll Specialist'", bool(ex.search("payroll specialist")))
check("include list unchanged by the merge (still has 'portfolio')", bool(sc._INCLUDE_RE.search("portfolio manager")))
# company blocklist
check("blocklist rejects 'Citadel Credit Union' and 'IMCO Carbide Tool Inc.'", sc._company_excluded("Citadel Credit Union") and sc._company_excluded("IMCO Carbide Tool Inc."))
check("blocklist does not reject 'Citadel' or 'Investment Management Corporation of Ontario'", not sc._company_excluded("Citadel") and not sc._company_excluded("Investment Management Corporation of Ontario"))
print("\n%d failure(s)" % len(fails)); sys.exit(1 if fails else 0)
