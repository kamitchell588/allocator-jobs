"""Fetch a few public careers pages and save what a scraper author needs to see (raw HTML, job links, script/API hints).
Read-only: it only downloads the pages listed in PAGES. Output goes to probe/<name>.* and is committed by the workflow.
Run: python probe_careers.py"""
import pathlib, re, sys
import requests

PAGES = {
    "citadel-list": "https://www.citadel.com/careers/open-opportunities/?sections-filter=investing&selected-job-sections=388%2C389%2C387%2C390&current_page=1&sort_order=DESC&per_page=20&action=careers_listing_filter",
    "citadel-job": "https://www.citadel.com/careers/details/investment-trading-intern-europe/",
    "deshaw-job": "https://www.deshaw.com/careers/mba-investment-analyst-new-york-summer-2027-6054",
    "deshaw-list": "https://www.deshaw.com/careers",
    "deshaw-internships": "https://www.deshaw.com/careers/internships",
    "twosigma-list": "https://careers.twosigma.com/careers/SearchJobs",
    "twosigma-job": "https://careers.twosigma.com/careers/JobDetail/New-York-New-York-United-States-Structured-Credit-Analyst/14350",
    "balyasny-list": "https://bambusdev.my.site.com/s/",
}
OUT = pathlib.Path(__file__).parent / "probe"
UA = {"User-Agent": "Mozilla/5.0 (compatible; allocator-jobs probe)"}


def main():
    OUT.mkdir(exist_ok=True)
    for name, url in PAGES.items():
        try:
            r = requests.get(url, headers=UA, timeout=30)
            html = r.text
            status = r.status_code
        except Exception as e:
            (OUT / f"{name}.txt").write_text(f"{url}\nFAILED: {type(e).__name__}: {e}\n"); print(name, "FAILED", e); continue
        links = sorted(set(re.findall(r'href="([^"]*(?:careers/[^"]*|apply[^"]*|greenhouse[^"]*|workday[^"]*|lever[^"]*)[^"]*)"', html)))[:200]
        scripts = sorted(set(re.findall(r'<script[^>]+src="([^"]+)"', html)))[:60]
        hints = sorted(set(re.findall(r'(admin-ajax\.php|wp-json/[\w/\-]+|/api/[\w/\-]+|ld\+json|"nonce"\s*:\s*"[^"]+"|ajax_url[^,;]{0,80})', html)))[:40]
        (OUT / f"{name}.txt").write_text(f"{url}\nstatus {status}, {len(html)} bytes\n\nHINTS\n" + "\n".join(hints) + "\n\nJOB/APPLY LINKS\n" + "\n".join(links) + "\n\nSCRIPTS\n" + "\n".join(scripts) + "\n")
        (OUT / f"{name}.html").write_text(html[:150000], encoding="utf-8")
        print(name, status, len(html), "bytes,", len(links), "links")


if __name__ == "__main__":
    main()
