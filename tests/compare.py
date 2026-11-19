"""Compare two pipeline output folders. Expected differences after step 1:
 (a) extra jobs that the old title+company dedup wrongly dropped (same title, different link);
 (b) a new 'status' column. Anything else is reported as UNEXPECTED and exits 1.
usage: python tests/compare.py <baseline_dir> <after_dir>"""
import csv, sys, pathlib
csv.field_size_limit(10**9)
def load(d): return list(csv.DictReader(open(pathlib.Path(d) / "allocator_jobs.csv", encoding="utf-8", newline="")))
a, b = load(sys.argv[1]), load(sys.argv[2])
k = lambda r: r["url"].lower().rstrip("/")
A, B = {k(r): r for r in a}, {k(r): r for r in b}
removed = [u for u in A if u not in B]
added = [u for u in B if u not in A]
title_co = {(r["title"].lower(), r["company"].lower()) for r in a}
explained = [u for u in added if (B[u]["title"].lower(), B[u]["company"].lower()) in title_co]
unexplained_add = [u for u in added if u not in explained]
ignore = {"status", "date_added"}
changed = [(u, c) for u in A if u in B for c in A[u] if c not in ignore and A[u][c] != B[u].get(c)]
print(f"baseline {len(a)} jobs, after {len(b)} jobs")
print(f"removed: {len(removed)}   added: {len(added)} (explained by dedup change: {len(explained)})   changed fields on shared jobs: {len(changed)}")
print("columns added:", [c for c in b[0] if c not in a[0]], " columns removed:", [c for c in a[0] if c not in b[0]])
print("status counts:", {s: sum(r.get('status') == s for r in b) for s in sorted({r.get('status') for r in b})})
for u in explained[:25]: print("  + kept:", B[u]["company"], "|", B[u]["title"], "|", B[u]["location"])
bad = removed or unexplained_add or changed
if bad:
    print("UNEXPECTED differences:", removed[:5], unexplained_add[:5], changed[:5]); sys.exit(1)
print("OK: only the intended differences.")
