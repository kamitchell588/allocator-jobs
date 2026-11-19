"""Run the whole pipeline on frozen inputs, in a throwaway copy of the repo, and save the outputs.
usage: python tests/run_pipeline.py <outdir>
No network: Apify fetch and supplemental scrapers are replaced with the frozen fixtures; link check is off."""
import contextlib, importlib.util, inspect, io, json, os, pathlib, shutil, sys, tempfile
REPO = pathlib.Path(__file__).resolve().parent.parent
FIX = REPO / "tests" / "fixtures"


def run(out, hidden_text=None, history_csv=None, include_text=None, allow_shrink=True, items=None):
    """Run main() on frozen inputs in a throwaway copy. Returns (exit_code, log_text)."""
    out = pathlib.Path(out); out.mkdir(parents=True, exist_ok=True)
    work = pathlib.Path(tempfile.mkdtemp())
    shutil.copy(REPO / "scraper.py", work / "scraper.py")
    shutil.copytree(REPO / "data", work / "data")
    shutil.copy(history_csv or FIX / "history.csv", work / "allocator_jobs.csv")
    if hidden_text is None:
        shutil.copy(FIX / "hidden_jobs.txt", work / "hidden_jobs.txt")
    else:
        (work / "hidden_jobs.txt").write_text(hidden_text)
    if include_text is not None:
        (work / "data" / "title_include.txt").write_text(include_text)
    os.environ["LINK_CHECK_MODE"] = "off"
    os.environ.pop("ALLOW_SHRINK", None)
    if allow_shrink:
        os.environ["ALLOW_SHRINK"] = "1"
    code, buf = 0, io.StringIO()
    cwd = os.getcwd(); os.chdir(work)   # scraper writes run_report.csv to the current folder
    with contextlib.redirect_stdout(buf):
        try:
            spec = importlib.util.spec_from_file_location("scraper_under_test", work / "scraper.py")
            s = importlib.util.module_from_spec(spec); spec.loader.exec_module(s)
            data = items if items is not None else json.load(open(FIX / "raw_items.json"))
            if "days" in inspect.signature(s.fetch_recent_items).parameters:
                s.fetch_recent_items = lambda days=1, now=None: list(data)
            else:
                s.fetch_recent_items = lambda now=None: list(data)
            s.scrape_all_supplemental = lambda: []
            s.main()
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
            if not isinstance(e.code, int): print(e.code)
    os.chdir(cwd)
    log = buf.getvalue()
    (out / "log.txt").write_text(log)
    for f in ["allocator_jobs.csv", "dashboard.html", "admin.html"]:
        if (work / f).exists(): shutil.copy(work / f, out / f)
    return code, log


if __name__ == "__main__":
    code, _ = run(sys.argv[1])
    print("exit", code, "- outputs in", sys.argv[1])
