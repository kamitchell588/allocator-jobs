import csv, pathlib, sys, tempfile, os
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import scraper as S

def job(i, **kw):
    d = {"title": f"Role {i}", "company": f"Firm {i}", "location": "NYC", "date_posted": "2026-11-20", "url": f"https://x.com/{i}/",
         "description": "", "job_type": "", "source": "", "seniority": "", "org_sector": "Private Equity", "metro": ""}
    d.update(kw); return d

jobs = [job(i) for i in range(8)]
picks = [("https://x.com/3", "Why three, with a comma, and \"quotes\""), ("https://x.com/1/", "One"), ("https://x.com/999", "Gone role"),
         ("https://x.com/5", ""), ("https://x.com/0", "Zero"), ("https://x.com/2", "Two"), ("https://x.com/4", "Four"), ("https://x.com/6", "Six")]
tmp = pathlib.Path(tempfile.mkdtemp()) / "data"; tmp.mkdir()
with open(tmp / "fram_selections.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f, quoting=csv.QUOTE_ALL); w.writerow(["url", "comment"]); w.writerows(picks + [("https://X.com/3/", "dup of three")])
S.__file__ = str(tmp.parent / "scraper.py")          # load_fram_picks reads data/ next to scraper.py
loaded = S.load_fram_picks()
assert [u for u, _ in loaded][:2] == ["https://x.com/3", "https://x.com/1/"], loaded
assert not any(u.endswith("/5") for u, _ in loaded), "no blurb -> ignored"
assert sum(1 for u, _ in loaded if u.lower().rstrip("/") == "https://x.com/3") == 1, "duplicates keep first"
got = S.fram_picks_for(jobs)
assert [p["job"]["title"] for p in got] == ["Role 3", "Role 1", "Role 0", "Role 2", "Role 4"], got   # file order, missing dropped, capped at 5
assert got[0]["blurb"] == 'Why three, with a comma, and "quotes"'
# a hidden role is not in the public jobs list, so it drops off and the next pick moves up
got2 = S.fram_picks_for([j for j in jobs if j["title"] != "Role 1"])
assert [p["job"]["title"] for p in got2] == ["Role 3", "Role 0", "Role 2", "Role 4", "Role 6"], got2
html = S._fram_strip_html(got)
assert "Fram Selections" in html and "Role 3" in html and "&quot;quotes&quot;" in html, html
assert S._fram_strip_html([]) == ""
rows = S._rows_html(jobs, {S._norm_url(p["job"]["url"]) for p in got})
assert rows.count('data-fram="1"') == 5 and rows.count('data-fram="0"') == 3
print("test_fram OK")
