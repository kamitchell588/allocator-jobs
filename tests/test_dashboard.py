import pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import scraper as S

assert S.short_location("New York, New York, United States") == "New York, New York"
assert S.short_location("Toronto, Ontario, Canada") == "Toronto, Ontario"
assert S.short_location("Boston, MA, USA; Chicago, IL; Austin, TX") == "Boston, MA +2"
assert S.short_location("Remote") == "Remote" and S.short_location("") == "" and S.short_location("United States") == "United States"
assert S.side_for({"company": "X", "org_sector": "Pension / Retirement"}) == "LP"
assert S.side_for({"company": "X", "org_sector": "Private Equity"}) == "GP"
assert S.side_for({"company": "X", "org_sector": "Wealth Management"}) == "" and S.side_for({"company": "X", "org_sector": ""}) == ""
S._SIDES = {"acme": "LP", "private equity": "GP"}            # a company row beats its sector row
assert S.side_for({"company": "Acme", "org_sector": "Private Equity"}) == "LP"
S._SIDES = None
row = S._rows_html([{"title": "Analyst", "company": "Acme", "location": "New York, NY, United States", "date_posted": "2026-11-20", "url": "http://x",
                     "description": "", "job_type": "FULL_TIME", "source": "", "seniority": "Not specified", "org_sector": "Private Equity", "metro": "New York"}])
assert 'data-side="GP"' in row and 'class="tag">Private Equity' in row and 'class="tag">Not specified' not in row and "FULL_TIME" not in row, row
# One display name per firm (orgs.csv search_term / aliases; leftover words must all be filler)
assert S.firm_name("Careers at KKR") == S.firm_name("Student Careers at KKR") == "KKR"
assert S.firm_name("General Atlantic - Campus") == S.firm_name("General Atlantic") == "General Atlantic"
assert S.firm_name("Bridgewater Associates Campus Recruiting") == S.firm_name("Bridgewater Associates LP") == "Bridgewater Associates"
assert S.firm_name("Apollo Information Systems") == "Apollo Information Systems"     # look-alike keeps its own name
assert S.firm_name("The Wilshire Group") == "The Wilshire Group" and S.firm_name("Wilshire Advisors LLC") == "Wilshire"
assert S.firm_name("Some Unlisted Firm LLC") == "Some Unlisted Firm LLC" and S.firm_name("") == ""
row = S._rows_html([{"title": "Analyst", "company": "Careers at KKR", "firm": "KKR", "location": "", "date_posted": "", "url": "", "description": ""}])
assert 'data-company="KKR"' in row and "<td>KKR</td>" in row, row
print("test_dashboard OK")
