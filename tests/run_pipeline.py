"""Run the whole pipeline on frozen inputs, in a throwaway copy of the repo, and save the outputs.
usage: python tests/run_pipeline.py <outdir>
No network: Apify fetch and supplemental scrapers are replaced with the frozen fixtures; link check is off."""
import json, os, pathlib, shutil, sys, tempfile, importlib.util, io, contextlib
REPO = pathlib.Path(__file__).resolve().parent.parent
FIX = REPO / "tests" / "fixtures"
out = pathlib.Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
work = pathlib.Path(tempfile.mkdtemp())
shutil.copy(REPO / "scraper.py", work / "scraper.py")
shutil.copytree(REPO / "data", work / "data")
shutil.copy(FIX / "history.csv", work / "allocator_jobs.csv")
shutil.copy(FIX / "hidden_jobs.txt", work / "hidden_jobs.txt")
os.environ["LINK_CHECK_MODE"] = "off"
os.environ.setdefault("ALLOW_SHRINK", "1")      # harness history is a different size than real runs
spec = importlib.util.spec_from_file_location("scraper_under_test", work / "scraper.py")
s = importlib.util.module_from_spec(spec); spec.loader.exec_module(s)
items = json.load(open(FIX / "raw_items.json"))
import inspect
if "days" in inspect.signature(s.fetch_recent_items).parameters:
    s.fetch_recent_items = lambda days=1, now=None: list(items)
else:
    s.fetch_recent_items = lambda now=None: list(items)
s.scrape_all_supplemental = lambda: []
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    try: s.main()
    except SystemExit as e: print("EXIT", e.code)
(out / "log.txt").write_text(buf.getvalue())
for f in ["allocator_jobs.csv", "dashboard.html", "admin.html"]:
    if (work / f).exists(): shutil.copy(work / f, out / f)
for f in (work / "data").glob("*report*"): shutil.copy(f, out / f.name)
print("outputs in", out)
