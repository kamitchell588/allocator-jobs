"""Scoped firms (data/scoped_orgs.csv): keyword scope replaces the generic title lists, and a cap keeps the newest."""
import pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import scraper as S

def item(t, loc="New York, New York, United States"):
    return {"title": t, "organization": "Goldman Sachs", "url": "https://x/" + t, "locations_derived": [loc]}

ok = item("Asset & Wealth Management, Alternative Investment Platform, Private Secondaries Group, Associate")
assert S.filter_jobs([ok]) == [ok], "alternatives role should pass (even though 'wealth management' is a generic exclusion)"
for bad in ["Asset & Wealth Management, Private Bank, Private Wealth Advisor",
            "Asset & Wealth Management, Software Engineering, Alternatives Platform Engineer",
            "Asset & Wealth Management, Controllers, Fund Accounting"]:
    assert S.filter_jobs([item(bad)]) == [], bad
assert S.filter_jobs([item("Private Equity Associate", "London, United Kingdom")]) == [], "non US/Canada must go"
other = {"title": "Investment Analyst", "organization": "Some Endowment", "url": "u", "locations_derived": ["Boston, Massachusetts, United States"]}
assert S.filter_jobs([other]) == [other], "unscoped firms use the normal lists"

jobs = [{"company": "Goldman Sachs", "title": f"t{i}", "date_posted": f"2026-11-{i+1:02d}"} for i in range(20)]
jobs.append({"company": "Other Fund", "title": "x", "date_posted": "2020-01-01"})
kept = S.apply_scope_caps(jobs)
gs = [j for j in kept if j["company"] == "Goldman Sachs"]
assert len(gs) == 15 and min(j["date_posted"] for j in gs) == "2026-11-06", "keeps the 15 newest"
assert any(j["company"] == "Other Fund" for j in kept), "other firms untouched"
print("test_scoped OK")
for bad in ["Investment Management, Project Manager", "Alternatives Platform Office Manager - New York"]:
    assert S.filter_jobs([item(bad)]) == [], bad
print("test_scoped (shared exclusions) OK")
S_jp = S.scope_for("JPMorganChase")
assert S_jp and S.scope_for("JPMorgan Chase & Co.") and S.scope_for("J.P. Morgan"), "JPM spellings share one scope"
assert S.scope_title_ok(S_jp, "Asset Management - Private Equity Investor Relations - Associate")
assert S.scope_title_ok(S_jp, "Asset Management, U.S. Private Markets Product Expansion Lead VP/ED")
assert not S.scope_title_ok(S_jp, "Investment Banking - Infrastructure M&A - Vice President"), "needs the Asset Management marker"
assert not S.scope_title_ok(S_jp, "Director of Infrastructure Engineering - AI"), "IT infrastructure is not alternatives"
assert not S.scope_title_ok(S_jp, "Home Lending Advisor")
print("test_scoped (JPM) OK")
