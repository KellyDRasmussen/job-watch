"""Diagnostic: for each URL in probe.txt, show what the page contains. Used when setting up employers."""
import re, json
from pathlib import Path
from playwright.sync_api import sync_playwright
from watch import JS_COLLECT, JOBLINK_RE, COOKIE_RE, click_matching

ATS = re.compile(r"greenhouse|lever\.co|ashbyhq|workable|teamtailor|emply|hr-manager|hrmanager|varbi|successfactors|myworkdayjobs|smartrecruiters|recruitee|personio|jobylon|taleo|oraclecloud|hr-skyen|talentech|zalaris", re.I)
urls = [u.strip() for u in Path("probe.txt").read_text().splitlines() if u.strip() and not u.startswith("#")]
out = []
with sync_playwright() as pw:
    b = pw.chromium.launch()
    ctx = b.new_context(locale="en-GB", user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
    for u in urls:
        p = ctx.new_page()
        r = {"url": u}
        try:
            resp = p.goto(u, wait_until="domcontentloaded", timeout=45000)
            r["status"] = resp.status if resp else None
            try: p.wait_for_load_state("networkidle", timeout=15000)
            except Exception: pass
            click_matching(p, COOKIE_RE, 1)
            p.mouse.wheel(0, 6000); p.wait_for_timeout(1500)
            r["final"] = p.url
            r["frames"] = [f.url for f in p.frames[1:]][:6]
            html = p.content()
            r["ats_hints"] = sorted(set(m.group(0).lower() for m in ATS.finditer(html)))[:8]
            links = []
            for f in p.frames:
                try: links += f.evaluate(JS_COLLECT)
                except Exception: pass
            r["n_links"] = len(links)
            jl = [l for l in links if JOBLINK_RE.search(l["href"]) and len(l["text"]) > 5]
            r["n_joblike"] = len(jl)
            r["sample"] = [(l["text"][:70], l["href"][:120]) for l in jl[:12]]
            r["ctx_example"] = jl[min(5, len(jl)-1)]["context"][:250] if jl else ""
            r["text_head"] = p.inner_text("body")[:400].replace("\n", " | ")
        except Exception as e:
            r["error"] = str(e)[:200]
        out.append(r); p.close()
    b.close()
Path("data").mkdir(exist_ok=True)
Path("data/probe.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))
