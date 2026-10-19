"""Job Watch: check employers' own careers pages and list job ads that are new since the last run.

Run:  python watch.py
Reads employers.yaml, keeps memory in data/seen.json, writes data/latest.json and data/latest.md.
It only finds and pre-filters links. Judging fit and knock-outs is done later by Claude.
"""
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import yaml
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).parent
DATA = ROOT / "data"
SEEN_FILE = DATA / "seen.json"

# Words in a job title that suggest one of Kelly's three role groups.
TITLE_KEYWORDS = [
    # data
    r"\bdata\b", r"analy", r"\bbi\b", r"business intelligence", r"power ?bi", r"insight", r"reporting",
    r"statistic", r"dashboard", r"\bpolicy\b", r"research (assistant|officer|coordinator|analyst)",
    # writing / comms
    r"communicat", r"\bcomms\b", r"content", r"writer", r"writing", r"editor", r"editorial", r"copy",
    r"journalis", r"newsletter", r"storytell", r"science (writer|communicat)", r"documentation",
    r"technical writ", r"\bpr\b", r"press", r"campaign", r"social media", r"marketing coordinator",
    # funding / programmes / community
    r"grant", r"fund", r"fundrais", r"partnership", r"programme", r"\bprogram\b", r"project (coordinator|manager|officer)",
    r"coordinator", r"community", r"engagement", r"talent", r"impact", r"evaluation", r"monitoring", r"\bmel\b",
    r"learning", r"knowledge", r"sustainab", r"\besg\b", r"operations (coordinator|officer|specialist)",
    r"international", r"stakeholder", r"secretariat", r"event",
]
TITLE_RE = re.compile("|".join(TITLE_KEYWORDS), re.I)

# Obvious no-go titles (teaching, sales, senior leadership, engineering etc.)
EXCLUDE_RE = re.compile(
    r"\b(sales|account executive|account manager|business development rep|teacher|lærer|"
    r"engineer|developer|technician|mechanic|electrician|operator|driver|chauffeur|nurse|"
    r"phd|postdoc|professor|lecturer|tenure|intern|internship|student assistant|studentermedhjælp|"
    r"vice president|\bvp\b|director|head of|chief)\b",
    re.I,
)

LOCATION_RE = re.compile(
    r"denmark|danmark|aarhus|århus|copenhagen|københavn|kobenhavn|hellerup|skanderborg|bjerringbro|"
    r"viby|brabrand|silkeborg|horsens|randers|\bremote\b|anywhere|europe \(remote\)|emea",
    re.I,
)

# URL looks like an individual job ad
JOBLINK_RE = re.compile(
    r"job|career|vacanc|position|stilling|requisition|posting|opening|/jobs?/|/vacancies/|"
    r"greenhouse|lever\.co|ashbyhq|workday|smartrecruiters|emply|hr-manager|hrmanager|varbi|"
    r"successfactors|teamtailor|recruitee|personio|jobdetails|jobid|vacancyno",
    re.I,
)

# Navigation links that are never job ads
NAV_TEXT_RE = re.compile(
    r"^(jobs?|careers?|vacancies|all jobs|see all|view all|search|apply|read more|læs mere|"
    r"home|about( us)?|contact|privacy|cookie|login|log in|sign in|next|previous|\d+|«|»|›|‹|"
    r"job alert|create job alert|students?|graduates?|life at .*|benefits|our culture)$",
    re.I,
)

CLICK_MORE_RE = re.compile(r"^(load more|show more|see more|vis flere|indlæs flere|more jobs)", re.I)
COOKIE_RE = re.compile(r"^(accept all|accept|allow all|tillad alle|accepter alle|accepter|ok|i agree|agree)$", re.I)

JS_COLLECT = """
() => {
  const out = [];
  for (const a of document.querySelectorAll('a[href]')) {
    const text = (a.innerText || a.textContent || '').replace(/\\s+/g, ' ').trim();
    let card = a, ctx = text;
    for (let i = 0; i < 5 && card.parentElement; i++) {
      card = card.parentElement;
      if (card.querySelectorAll('a[href]').length > 2) break;
      const t = (card.innerText || '').replace(/\\s+/g, ' ').trim();
      if (t.length > 600) break;
      ctx = t;
    }
    out.push({href: a.href, text: text.slice(0, 200), context: ctx.slice(0, 600)});
  }
  return out;
}
"""


def load_seen():
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text())
    return {}


def norm_url(u):
    p = urlparse(u)
    # drop tracking params and fragments but keep ids in the query
    q = "&".join(x for x in p.query.split("&") if x and not x.lower().startswith(("utm_", "gh_src", "source=", "ref=")))
    return f"{p.scheme}://{p.netloc.lower()}{p.path.rstrip('/')}" + (f"?{q}" if q else "")


def click_matching(page, rx, limit):
    clicked = 0
    for _ in range(limit):
        found = False
        for el in page.query_selector_all("button, a[role=button], a"):
            try:
                t = (el.inner_text() or "").strip()
            except Exception:
                continue
            if t and rx.match(t) and el.is_visible():
                try:
                    el.click(timeout=3000)
                    page.wait_for_timeout(1500)
                    clicked += 1
                    found = True
                    break
                except Exception:
                    continue
        if not found:
            break
    return clicked


def scrape(page, emp):
    page.goto(emp["url"], wait_until="domcontentloaded", timeout=45000)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    click_matching(page, COOKIE_RE, 1)
    for _ in range(4):
        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(700)
    click_matching(page, CLICK_MORE_RE, 3)
    links = []
    for frame in page.frames:
        try:
            links += frame.evaluate(JS_COLLECT)
        except Exception:
            pass
    return links, page.url


def filter_links(emp, links, final_url):
    own_host = urlparse(final_url).netloc
    custom = re.compile(emp["link_regex"], re.I) if emp.get("link_regex") else None
    out, seen_here = [], set()
    candidates = 0
    for l in links:
        href, text, ctx = l["href"], l["text"], l["context"]
        if not href.startswith("http") or not text or len(text) < 6:
            continue
        if NAV_TEXT_RE.match(text):
            continue
        if custom:
            if not custom.search(href):
                continue
        elif not JOBLINK_RE.search(href):
            continue
        if norm_url(href) == norm_url(final_url):
            continue
        candidates += 1
        title = text.split(" | ")[0][:150]
        if EXCLUDE_RE.search(title):
            continue
        if not emp.get("all_titles") and not TITLE_RE.search(title):
            continue
        if emp.get("global") and not LOCATION_RE.search(ctx):
            continue
        key = norm_url(href)
        if key in seen_here:
            continue
        seen_here.add(key)
        out.append({"employer": emp["name"], "area": emp.get("area", ""), "title": title,
                    "url": href, "context": ctx[:300]})
    return out, candidates


def main():
    DATA.mkdir(exist_ok=True)
    employers = yaml.safe_load((ROOT / "employers.yaml").read_text())["employers"]
    only = sys.argv[1:]  # optional: names to test
    if only:
        employers = [e for e in employers if any(o.lower() in e["name"].lower() for o in only)]
    seen = load_seen()
    first_run = not seen
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    new, status = [], []

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(
            locale="en-GB",
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            viewport={"width": 1366, "height": 900},
        )
        for emp in employers:
            page = ctx.new_page()
            st = {"employer": emp["name"], "url": emp["url"], "links": 0, "job_like": 0, "kept": 0, "new": 0, "error": ""}
            try:
                links, final_url = scrape(page, emp)
                st["links"] = len(links)
                kept, st["job_like"] = filter_links(emp, links, final_url)
                st["kept"] = len(kept)
                for job in kept:
                    key = norm_url(job["url"])
                    if key not in seen:
                        seen[key] = {"first_seen": today, "employer": job["employer"], "title": job["title"]}
                        new.append(job)
                        st["new"] += 1
            except Exception as e:  # keep going, report it
                st["error"] = f"{type(e).__name__}: {str(e)[:200]}"
            if not st["error"] and st["job_like"] == 0:
                st["error"] = "No job links found: page may need a different URL or a link_regex"
            status.append(st)
            print(f"{emp['name']:<45} links={st['links']:<4} job_like={st['job_like']:<4} kept={st['kept']:<3} new={st['new']:<3} {st['error']}")
            page.close()
        browser.close()

    if not only:
        SEEN_FILE.write_text(json.dumps(seen, indent=1, ensure_ascii=False, sort_keys=True))
    result = {"run_date": today, "first_run": first_run, "new": new, "status": status}
    out_json = DATA / ("test.json" if only else "latest.json")
    out_json.write_text(json.dumps(result, indent=1, ensure_ascii=False))

    lines = [f"# Job Watch {today}", "", f"New ads: {len(new)}" + (" (first run: everything counts as new)" if first_run else ""), ""]
    for j in new:
        lines.append(f"- **{j['employer']}**: [{j['title']}]({j['url']})")
    broken = [s for s in status if s["error"]]
    if broken:
        lines += ["", "## Pages that need attention", ""]
        lines += [f"- {s['employer']}: {s['error']} ({s['url']})" for s in broken]
    (DATA / ("test.md" if only else "latest.md")).write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
