"""Print every title the current rules rank Executive / Head or leave Not specified, from allocator_jobs.csv, for a manual check.
Run: python tests/seniority_review.py   (edit the rules in scraper.py: seniority_label)"""
import csv, pathlib, sys, collections
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from scraper import seniority_label, SENIORITY_LEVELS
rows = list(csv.DictReader(open(pathlib.Path(__file__).resolve().parent.parent / "allocator_jobs.csv", encoding="utf-8")))
by = collections.defaultdict(list)
for r in rows:
    by[seniority_label(r["title"], r.get("experience_years", ""))].append(f'{r["title"].strip()}  [{r["company"].strip()}]')
for level in (SENIORITY_LEVELS[4], "Not specified"):
    print(f"\n== {level} ({len(by[level])}) ==")
    for t in sorted(by[level]): print("  ", t)
