"""Live board for the sundae_bar Lab (SN121).

Reads the live round's public submissions pages in a hidden browser and shows a
dashboard: ranking, waiting queue, failed entries. Click a score for the per-sample
grades, a submission name to download its skill bundle, a Sim. other value for the
closest other submission.

    python live_board.py                 # local server at http://127.0.0.1:8121, re-reads every 60 s
    python live_board.py --round 40      # a specific round
    python live_board.py --every 30 --port 8121
    python live_board.py --once site     # one read: writes site/index.html, data.json, details.json (GitHub Pages)
    python live_board.py --once site --prev https://<you>.github.io/<repo>/details.json   # reuse the last details

Needs Playwright with Chromium (`pip install playwright` then `python -m playwright install chromium`).
"""
import argparse, json, os, sys, threading, time, datetime, traceback, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from playwright.sync_api import sync_playwright

SITE = "https://www.sundaebar.ai"
DONE = {"scored", "failed"}
STATE = {"data": None, "error": None, "busy": False}
DETAILS = {}                      # submission id -> trimmed per-sample grades (fixed once scored)
LOCK = threading.Lock()
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".board_cache.json")


# ---------------------------------------------------------------- reading the Lab

def scrape(pg, want_round):
    got = {}

    def on(r):
        u = r.url
        if "api.sundaebar.ai/api/v2/" not in u:
            return
        try:
            if "challenges?limit=" in u:
                got["challenges"] = r.json()
            elif "/submissions?" in u and "limit=10" in u:
                got[u] = r.json()
        except Exception:
            pass

    pg.on("response", on)
    try:
        v = want_round
        if v is None:
            pg.goto(f"{SITE}/lab/challenges", wait_until="networkidle", timeout=90000)
            chs = (got.get("challenges") or {}).get("challenges") or []
            if not chs:
                raise RuntimeError("Could not read the challenge list from the Lab")
            live = [c for c in chs if c.get("status") == "active" or c.get("submissions_open")]
            v = max(c["version"] for c in (live or chs))
        pg.goto(f"{SITE}/lab/challenges/sbc{v}", wait_until="networkidle", timeout=90000)
        base = pg.url.split("?")[0].split("#")[0].rstrip("/")
        if "/lab/challenges/sbc" not in base:
            raise RuntimeError(f"Round SBC{v} not found")
        for k in [k for k in got if k.startswith("http")]:
            got.pop(k)
        pg.goto(base + "/submissions", wait_until="networkidle", timeout=90000)
        pg.wait_for_timeout(800)
        nxt = pg.locator('button[aria-label="Go to next page"]').first
        for _ in range(300):
            try:
                if nxt.count() == 0 or nxt.is_disabled():
                    break
            except Exception:
                break
            before = sum(k.startswith("http") for k in got)
            nxt.click()
            for _ in range(60):
                pg.wait_for_timeout(200)
                if sum(k.startswith("http") for k in got) > before:
                    break
            else:
                break
        subs, meta, seen = [], {}, set()
        for k, val in got.items():
            if k.startswith("http"):
                meta = {x: val.get(x) for x in val if x != "submissions"}
                for s in val.get("submissions", []):
                    if s["id"] not in seen:
                        seen.add(s["id"])
                        subs.append(s)
        ch = next((c for c in (got.get("challenges") or {}).get("challenges", []) if c.get("version") == v), {})
        return summarise(v, base, subs, meta, ch)
    finally:
        pg.remove_listener("response", on)


def trim_tasks(v):
    """Keep what the score popup shows from the site's per-submission task record."""
    out = []
    for t in (v or {}).get("tasks") or []:
        rd = t.get("result_data") or {}
        samples = []
        for r in rd.get("results") or []:
            grades = {}
            for k, g in (r.get("grades_by_key") or {}).items():
                g = g or {}
                grades[k] = {"score": g.get("score"), "note": g.get("feedback") or g.get("rationale") or ""}
            samples.append({"id": r.get("id"), "score": r.get("score"), "tokens": r.get("tokens"),
                            "grades": grades, "refs": [x.get("path") for x in r.get("references_read") or []]})
        out.append({
            "validator": t.get("validator_display_name") or "", "status": t.get("status"), "score": t.get("score"),
            "verdict": (rd.get("scoring") or {}).get("verdict"), "tokens": rd.get("total_tokens"),
            "seconds": rd.get("duration_seconds"), "completed": (t.get("completed_at") or "")[:19].replace("T", " "),
            "reads": (rd.get("skill_bundle") or {}).get("reference_reads") or {},
            "failure": t.get("failure_reason") or t.get("error_message") or "",
            "withheld": bool(t.get("rationales_withheld")), "samples": samples})
    return out


def fetch_details(pg, base, rows, limit, budget_s, pause_s=3.0):
    """Open each new finished submission's page once and keep its grades.

    The site rate-limits these requests (HTTP 429 after a few dozen in a row), so this
    goes slowly, reads at most `limit` per call and stops at the first 429; the rest
    are read on later updates.
    """
    todo = [r for r in rows if r["status"] in DONE and r["id"] not in DETAILS][:limit]
    t0, n = time.time(), 0
    for i, r in enumerate(todo):
        if time.time() - t0 > budget_s:
            break
        if i:
            pg.wait_for_timeout(int(pause_s * 1000))
        sid = r["id"]
        try:
            with pg.expect_response(lambda x: f"tasks?submission_id={sid}" in x.url and x.request.method == "GET",
                                    timeout=30000) as ri:
                pg.goto(f"{base}/submissions/{sid}", wait_until="commit", timeout=60000)
            resp = ri.value
            if resp.status == 429:
                print("  details: the site is rate-limiting; the rest wait for the next update", file=sys.stderr, flush=True)
                break
            if resp.status != 200:
                continue
            DETAILS[sid] = trim_tasks(resp.json())
            n += 1
        except Exception as e:
            print(f"  details for {r['submission']} not read: {type(e).__name__}", file=sys.stderr, flush=True)
    return n


def summarise(v, url, S, m, c):
    lead_id = m.get("current_leader_submission_id")
    scored = sorted([s for s in S if s["validation_status"] == "scored" and s.get("total_score") is not None],
                    key=lambda s: -s["total_score"])
    pos = {s["id"]: i + 1 for i, s in enumerate(scored)}
    pct = lambda x: None if x is None else round(x * 100, 3)
    rows = []
    for s in S:
        data = s.get("data") or {}
        files = [{"path": f.get("path") or f.get("name"), "url": f.get("url"), "size": f.get("size")}
                 for f in data.get("skill_files") or [] if f.get("url")]
        sc = data.get("shared_content") or {}
        rows.append(dict(
            id=s["id"], pos=pos.get(s["id"]), submission=s.get("display_name"), username=s.get("username") or "",
            uid=s.get("miner_uid"), hotkey=s.get("miner_hotkey") or "", score=pct(s.get("total_score")),
            status=s.get("validation_status"), integrity=s.get("integrity_status") or "",
            integrity_reason=s.get("integrity_public_reason") or "",
            leader=s["id"] == lead_id, submitted=(s.get("created_at") or "")[:19].replace("T", " "),
            sim_other=s.get("similarity_score_other"), sim_other_id=s.get("similarity_matched_other_id"),
            sim_own=s.get("similarity_score_own"), sim_own_id=s.get("similarity_matched_own_id"),
            shared={k: sc.get(k) for k in ("shared", "ours_in_theirs", "theirs_in_ours", "matched_submission_id")} if sc else None,
            files=files))
    L = next((r for r in rows if r["leader"]), None)
    return dict(
        round=f"SBC{v}", title=c.get("title") or "", url=url,
        opens=(c.get("submissions_open_at") or "")[:16].replace("T", " "),
        closes=(c.get("submissions_close_at") or "")[:16].replace("T", " "),
        open=bool(c.get("submissions_open")), count=m.get("count", len(S)),
        scored=sum(r["status"] == "scored" for r in rows), failed=sum(r["status"] == "failed" for r in rows),
        waiting=sum(r["status"] not in DONE for r in rows), miners=len({r["hotkey"] for r in rows}),
        top=pct(m.get("top_score")), margin=pct(m.get("leadership_margin") or 0), leader=L,
        fetched=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        rows=rows)


# ---------------------------------------------------------------- local server mode

def load_cache():
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            DETAILS.update(json.load(f))
    except Exception:
        pass


def save_cache():
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(DETAILS, f)
    except Exception:
        pass


def worker(args, wake):
    load_cache()
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_page(viewport={"width": 1400, "height": 1800})
        while True:
            with LOCK:
                STATE["busy"] = True
            t = time.time()
            try:
                d = scrape(pg, args.round)
                with LOCK:
                    STATE["data"], STATE["error"] = d, None
                n = fetch_details(pg, d["url"], d["rows"], limit=12, budget_s=90)
                if n:
                    save_cache()
                left = sum(r["status"] in DONE and r["id"] not in DETAILS for r in d["rows"])
                print(f"[{d['fetched']} UTC] {d['round']}: {d['count']} submissions, {d['waiting']} waiting, "
                      f"leader {d['leader'] and d['leader']['username']}; details +{n}"
                      f"{f' ({left} still to read)' if left else ''} ({time.time() - t:.0f}s)", flush=True)
            except Exception as e:
                traceback.print_exc()
                with LOCK:
                    STATE["error"] = f"{type(e).__name__}: {e}"
                try:
                    pg.close()
                except Exception:
                    pass
                pg = b.new_page(viewport={"width": 1400, "height": 1800})
            with LOCK:
                STATE["busy"] = False
            wake.wait(args.every)
            wake.clear()


class H(BaseHTTPRequestHandler):
    wake = None

    def log_message(self, *a):
        pass

    def send(self, code, body, ctype):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/data.json":
            with LOCK:
                self.send(200, json.dumps(STATE), "application/json")
        elif path == "/details.json":
            with LOCK:
                self.send(200, json.dumps(DETAILS), "application/json")
        elif path == "/refresh":
            H.wake.set()
            self.send(200, "{}", "application/json")
        elif path in ("/", "/index.html"):
            self.send(200, PAGE, "text/html; charset=utf-8")
        else:
            self.send(404, "not found", "text/plain")


# ---------------------------------------------------------------- the page

PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SN121 Live Board</title>
<style>
:root{--bg:#f6f7f9;--panel:#fff;--fg:#16191f;--mut:#5d6573;--line:#dfe3ea;--ok:#13804b;--warn:#b26a00;--bad:#c2312f;--lead:#e7f5ed;--accent:#2f5bd3;--shade:rgba(15,18,25,.45)}
@media (prefers-color-scheme:dark){:root{--bg:#111318;--panel:#1a1d24;--fg:#e8eaef;--mut:#9aa3b2;--line:#2c313b;--ok:#4cc38a;--warn:#e7a43b;--bad:#f07070;--lead:#173326;--accent:#7d9cf0;--shade:rgba(0,0,0,.6);color-scheme:dark}}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.w{max-width:1500px;margin:0 auto;padding:20px max(16px,env(safe-area-inset-left))}
h1{font-size:22px;margin:0 0 4px}.lede{color:var(--mut);margin:0 0 14px}.lede b{color:var(--fg)}
.bar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:14px;font-size:13px;color:var(--mut)}
.bar input[type=search]{font:inherit;padding:5px 8px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--fg);width:220px;max-width:100%}
button.b{font:inherit;padding:5px 12px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--fg);cursor:pointer}
button.b.pri{background:var(--accent);border-color:var(--accent);color:#fff}
.asof{margin-left:auto}.err{color:var(--bad)}.gst{color:var(--mut);font-size:12.5px}.gst a{margin-left:4px}button.b:disabled{opacity:.6;cursor:default}
.tk input{font:inherit;width:100%;padding:6px 10px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg)}.tk ol{margin:0;padding-left:20px}.tk li{margin:3px 0}.tk .row{display:flex;gap:8px;flex-wrap:wrap}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:16px}
.t{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px 14px;min-width:0}
.t .k{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut)}.t .v{font-size:22px;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}.t .s{font-size:12px;color:var(--mut);overflow-wrap:anywhere}
.two{display:grid;grid-template-columns:minmax(0,3fr) minmax(0,2fr);gap:16px;align-items:start}@media(max-width:950px){.two{grid-template-columns:minmax(0,1fr)}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:14px;min-width:0;margin-bottom:16px}
.card h2{font-size:16px;margin:0 0 4px}.card h2 small{font-weight:400;color:var(--mut);font-size:12px;margin-left:6px}.note{font-size:12px;color:var(--mut);margin:0 0 8px}
.sc{overflow:auto;max-height:640px}.sc.s{max-height:360px}
table{width:100%;border-collapse:collapse;font-size:12.5px;font-variant-numeric:tabular-nums}
th{position:sticky;top:0;background:var(--panel);text-align:left;color:var(--mut);font-weight:600;padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}.n{text-align:right}
tr.lead td{background:var(--lead)}tr.new td:first-child{box-shadow:inset 3px 0 0 var(--accent)}
.hk{font-family:ui-monospace,Consolas,monospace;cursor:copy;border-bottom:1px dotted var(--mut)}
.lk{background:none;border:0;padding:0;font:inherit;color:var(--accent);cursor:pointer;text-decoration:underline dotted;text-underline-offset:3px}
.lk:hover{text-decoration-style:solid}.lk:focus-visible,.hk:focus-visible,button.b:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.pill{display:inline-block;padding:0 8px;border-radius:999px;border:1px solid currentColor;font-size:11px;line-height:17px}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.mut{color:var(--mut)}.empty{color:var(--mut);padding:10px;white-space:normal}
a{color:var(--accent)}
dialog{border:1px solid var(--line);border-radius:10px;background:var(--panel);color:var(--fg);padding:0;width:min(1100px,calc(100vw - 32px));max-height:calc(100vh - 48px)}
dialog::backdrop{background:var(--shade)}
.mh{display:flex;align-items:flex-start;gap:12px;padding:14px 18px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--panel);z-index:2}
.mh h3{margin:0;font-size:17px}.mh p{margin:2px 0 0;color:var(--mut);font-size:12.5px;overflow-wrap:anywhere}.mh .x{margin-left:auto}
.mb{padding:14px 18px 18px;display:grid;gap:14px}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px}
.kv div{border:1px solid var(--line);border-radius:6px;padding:6px 10px;min-width:0}.kv b{display:block;font-size:11px;color:var(--mut);font-weight:500;text-transform:uppercase;letter-spacing:.04em}.kv span{font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.g{display:inline-block;min-width:44px;text-align:right}
.g.hi{color:var(--ok)}.g.md{color:var(--warn)}.g.lo{color:var(--bad)}
tr.samp{cursor:pointer}tr.samp:hover td{background:color-mix(in srgb,var(--accent) 7%,transparent)}
tr.notes td{white-space:normal;color:var(--mut);font-size:12px;background:var(--bg)}
tr.notes ul{margin:4px 0;padding-left:18px}tr.notes li{margin:2px 0}
tr.avg td{font-weight:600;border-top:2px solid var(--line)}
.files td a{word-break:break-all}
.who{border:1px solid var(--line);border-radius:8px;padding:10px 14px;display:grid;gap:4px}
.who .nm{font-size:16px;font-weight:600}
</style></head><body><div class="w">
<h1>SN121 Live Board <span id="rname"></span></h1>
<p class="lede" id="lede">Loading the Lab… the first read takes about a minute.</p>
<div class="bar"><label><input type="checkbox" id="best"> Best per miner</label>
<input type="search" id="q" placeholder="Filter user, UID or hotkey" aria-label="Filter">
<button class="b" id="now" type="button">Read the Lab now</button>
<span id="gst" class="gst" aria-live="polite"></span><button class="lk" id="gtk" type="button" hidden>GitHub token</button>
<span class="asof" id="asof"></span></div>
<div class="tiles" id="tiles"></div>
<div class="two"><section class="card"><h2>Ranking <small id="rc"></small></h2><p class="note">Click a <b>score</b> for per-sample grades, a <b>submission</b> to download its skill, a <b>Sim. other</b> value for the closest other submission. Green = current leader; blue edge = submitted in the last hour.</p>
<div class="sc"><table><thead><tr><th class="n">#</th><th>User</th><th class="n">UID</th><th>Hotkey</th><th class="n">Score</th><th>Integrity</th><th>Submission</th><th>Submitted (UTC)</th><th class="n">Sim. other</th></tr></thead><tbody id="rank"></tbody></table></div></section>
<div><section class="card"><h2>Waiting <small id="wc"></small></h2><p class="note">Uploaded, not scored yet, oldest first.</p>
<div class="sc s"><table><thead><tr><th class="n">Q</th><th>User</th><th class="n">UID</th><th>Hotkey</th><th>Status</th><th>Submitted (UTC)</th></tr></thead><tbody id="wait"></tbody></table></div></section>
<section class="card"><h2>Failed <small id="fc"></small></h2>
<div class="sc s"><table><thead><tr><th>User</th><th class="n">UID</th><th>Hotkey</th><th>Submission</th><th>Submitted (UTC)</th></tr></thead><tbody id="fail"></tbody></table></div></section></div></div>
</div>
<dialog id="dlg" aria-labelledby="dlg-t"><div class="mh"><div><h3 id="dlg-t"></h3><p id="dlg-s"></p></div><button class="b x" id="dlg-x" type="button" aria-label="Close">Close</button></div><div class="mb" id="dlg-b"></div></dialog>
<script src="https://cdnjs.cloudflare.com/ajax/libs/jszip/3.10.1/jszip.min.js"></script>
<script>
const STATIC=false;
let D=null,ERR=null,BUSY=false,DET=null,DET_AT=0;const $=id=>document.getElementById(id);
const user=u=>u?'@'+u:'(no username)',sh=h=>h&&h.length>14?h.slice(0,6)+'…'+h.slice(-5):(h||''),f3=v=>v==null?'—':(+v).toFixed(3);
const pc=v=>v==null?'—':(v*100).toFixed(1),kb=b=>b==null?'—':b<1024?b+' B':(b/1024).toFixed(1)+' KB';
function el(t,txt,cls){const e=document.createElement(t);if(txt!=null)e.textContent=txt;if(cls)e.className=cls;return e}
function hk(h){const s=el('span',sh(h),'hk');s.title=h;s.tabIndex=0;s.setAttribute('role','button');s.setAttribute('aria-label','Copy hotkey '+h);
 const go=()=>navigator.clipboard&&navigator.clipboard.writeText(h).then(()=>{s.textContent='copied';setTimeout(()=>s.textContent=sh(h),900)});s.onclick=go;s.onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();go()}};return s}
function lk(txt,fn,label){const b=el('button',txt,'lk');b.type='button';if(label)b.setAttribute('aria-label',label);b.onclick=fn;return b}
function pill(t,c){return el('span',t,'pill '+c)}
function row(cells,cls){const tr=el('tr',null,cls);for(const [v,c] of cells){const td=el('td',null,c);if(v instanceof Node)td.appendChild(v);else td.textContent=v??'';tr.appendChild(td)}return tr}
function fill(id,rows,cols,msg){const tb=$(id);tb.replaceChildren(...(rows.length?rows:[(()=>{const tr=el('tr');const td=el('td',msg,'empty');td.colSpan=cols;tr.appendChild(td);return tr})()]))}
function match(r){const q=$('q').value.trim().toLowerCase();return !q||[r.username,r.uid,r.hotkey,r.submission].some(x=>String(x??'').toLowerCase().includes(q))}
function recent(r){return D&&(Date.parse(D.fetched.replace(' ','T')+'Z')-Date.parse(r.submitted.replace(' ','T')+'Z'))<3600e3}
const byId=id=>D&&D.rows.find(r=>r.id===id);

/* ---------- modal ---------- */
function openModal(title,sub,nodes){$('dlg-t').textContent=title;$('dlg-s').textContent=sub||'';$('dlg-b').replaceChildren(...nodes);const d=$('dlg');if(!d.open)d.showModal();$('dlg-b').scrollTop=0}
$('dlg-x').onclick=()=>$('dlg').close();$('dlg').addEventListener('click',e=>{if(e.target===$('dlg'))$('dlg').close()});
function kv(pairs){const g=el('div',null,'kv');for(const [k,v] of pairs){const d=el('div');d.append(el('b',k),el('span',v==null||v===''?'—':String(v)));g.appendChild(d)}return g}
async function details(force){if(!DET||force||Date.now()-DET_AT>60000){try{DET=await (await fetch((STATIC?'details.json?t=':'/details.json?t=')+Date.now(),{cache:'no-store'})).json();DET_AT=Date.now()}catch(e){DET=DET||{}}}return DET}

const GRADES=[['rubric','Rubric'],['skill_use','Skill use'],['novelty_check','Novelty'],['scenario_quality','Scenario'],['dataset_derived','Dataset'],['skill_alignment','Alignment'],['skill_redundancy','Redundancy']];
function gcell(v){const s=el('span',v==null?'—':(+v).toFixed(3),'g'+(v==null?'':v>=.85?' hi':v>=.6?' md':' lo'));return s}
async function showScore(r){
 openModal('Score detail · '+r.submission,user(r.username)+' · UID '+r.uid+' · total '+f3(r.score),[el('p','Loading…','note')]);
 const all=await details();const T=(all[r.id]||[]).filter(t=>t.samples&&t.samples.length);
 if(!T.length){openModal('Score detail · '+r.submission,user(r.username)+' · UID '+r.uid+' · total '+f3(r.score),[el('p',r.status==='scored'?'Per-sample grades for this submission have not been read yet. They are read once per submission after scoring; check again after the next update.':'No grades: this submission is '+r.status+'.','note')]);return}
 const out=[];
 for(const t of T){
  out.push(kv([['Validator',t.validator],['Score',f3(t.score*100)],['Verdict',t.verdict],['Samples',t.samples.length],['Tokens',t.tokens!=null?t.tokens.toLocaleString():'—'],['Eval time',t.seconds!=null?Math.round(t.seconds)+' s':'—'],['Completed (UTC)',t.completed],['Reference reads',Object.entries(t.reads||{}).map(([k,v])=>k+' ×'+v).join(', ')||'none']]));
  const keys=GRADES.filter(([k])=>t.samples.some(s=>s.grades[k]));
  const extra=[...new Set(t.samples.flatMap(s=>Object.keys(s.grades)))].filter(k=>!GRADES.some(([g])=>g===k)).map(k=>[k,k]);
  const cols=keys.concat(extra);
  const tbl=el('table');const th=el('tr');[['Sample'],['Score','n'],...cols.map(([,l])=>[l,'n']),['Tokens','n'],['Refs read']].forEach(([l,c])=>th.appendChild(el('th',l,c)));
  const thead=el('thead');thead.appendChild(th);tbl.appendChild(thead);const tb=el('tbody');
  for(const s of t.samples){
   const tr=row([[s.id||'—'],[gcell(s.score),'n'],...cols.map(([k])=>[gcell(s.grades[k]&&s.grades[k].score),'n']),[s.tokens!=null?s.tokens.toLocaleString():'—','n'],[(s.refs||[]).join(', ')||'—']],'samp');
   tr.tabIndex=0;tr.title='Show judge notes';
   const notes=el('tr',null,'notes');notes.hidden=true;const td=el('td');td.colSpan=cols.length+4;const ul=el('ul');
   for(const [k,l] of cols){const g=s.grades[k];if(g&&g.note){const li=el('li');li.append(el('b',l+': '),document.createTextNode(g.note));ul.appendChild(li)}}
   td.appendChild(ul.children.length?ul:el('span',t.withheld?'Judge notes are withheld by the site for this submission.':'No notes.'));notes.appendChild(td);
   const tog=()=>{notes.hidden=!notes.hidden};tr.onclick=tog;tr.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();tog()}};
   tb.append(tr,notes)}
  const avg=k=>{const v=t.samples.map(s=>k?s.grades[k]&&s.grades[k].score:s.score).filter(x=>x!=null);return v.length?v.reduce((a,b)=>a+b,0)/v.length:null};
  tb.appendChild(row([['Average'],[gcell(avg(null)),'n'],...cols.map(([k])=>[gcell(avg(k)),'n']),[t.samples.reduce((a,s)=>a+(s.tokens||0),0).toLocaleString(),'n'],['']],'avg'));
  tbl.appendChild(tb);const sc=el('div',null,'sc');sc.style.maxHeight='none';sc.appendChild(tbl);out.push(sc);
  out.push(el('p','Grades are 0–1 per sample. Click a sample row to show the judges’ notes. The total is the site’s own weighting of these grades, so it is not a plain average of the columns.','note'));
 }
 openModal('Score detail · '+r.submission,user(r.username)+' · UID '+r.uid+' · total '+f3(r.score),out);
}

function fileName(r){return (r.submission||'skill')+'.zip'}
async function zipAll(r,btn,msg){
 if(!window.JSZip){msg.textContent='The zip library did not load; download the files one by one below.';return}
 btn.disabled=true;msg.textContent='Fetching '+r.files.length+' files…';
 try{const z=new JSZip();for(const f of r.files){const res=await fetch(f.url);if(!res.ok)throw new Error(f.path+': HTTP '+res.status);z.file(f.path,await res.blob())}
  const blob=await z.generateAsync({type:'blob'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=fileName(r);document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(a.href),5000);
  msg.textContent='Saved '+fileName(r)+'.'}catch(e){msg.textContent='Could not build the zip ('+e.message+'). Use the links below.'}
 btn.disabled=false}
function showFiles(r){
 const out=[kv([['User',user(r.username)],['UID',r.uid],['Score',f3(r.score)],['Submitted (UTC)',r.submitted],['Files',r.files.length],['Total size',kb(r.files.reduce((a,f)=>a+(f.size||0),0))]])];
 if(!r.files.length){out.push(el('p','The site lists no files for this submission.','note'));openModal(r.submission,user(r.username),out);return}
 const bar=el('div');bar.style.cssText='display:flex;gap:10px;align-items:center;flex-wrap:wrap';const btn=el('button','Download '+fileName(r),'b pri');btn.type='button';const msg=el('span','','note');btn.onclick=()=>zipAll(r,btn,msg);bar.append(btn,msg);out.push(bar);
 const tbl=el('table','', 'files');const h=el('tr');['File','Size',''].forEach((l,i)=>h.appendChild(el('th',l,i===1?'n':'')));const th=el('thead');th.appendChild(h);tbl.appendChild(th);const tb=el('tbody');
 for(const f of r.files){const a=el('a',f.path);a.href=f.url;a.target='_blank';a.rel='noopener';const o=el('a','open');o.href=f.url;o.target='_blank';o.rel='noopener';tb.appendChild(row([[a],[kb(f.size),'n'],[o]]))}
 tbl.appendChild(tb);out.push(tbl);out.push(el('p','The zip keeps the bundle layout (SKILL.md at the top, references/ beside it), the same shape the submit form takes.','note'));
 openModal(r.submission,user(r.username)+' · UID '+r.uid+' · '+r.status,out);
}

function whoCard(label,m,id,sim){
 const c=el('div',null,'who');c.appendChild(el('span',label,'note'));
 if(m){c.appendChild(el('span',user(m.username),'nm'));
  c.appendChild(kv([['Submission',m.submission],['UID',m.uid],['Hotkey',m.hotkey],['Score',f3(m.score)],['Submitted (UTC)',m.submitted],['Similarity',sim==null?'—':(+sim).toFixed(4)]]));
  const btns=el('div');btns.style.cssText='display:flex;gap:12px;flex-wrap:wrap';btns.append(lk('Score detail',()=>showScore(m)),lk('Download',()=>showFiles(m)));c.appendChild(btns)}
 else c.appendChild(el('span',id?'Submission '+id+' is not in this round (likely an earlier round), so its owner is not shown here.':'No match recorded.','note'));
 return c}
function showSim(r){
 const o=r.sim_other_id&&byId(r.sim_other_id),w=r.sim_own_id&&byId(r.sim_own_id);const out=[];
 out.push(kv([['Sim. to others',r.sim_other==null?'—':(+r.sim_other).toFixed(4)],['Sim. to own',r.sim_own==null?'—':(+r.sim_own).toFixed(4)],['Integrity',r.integrity+(r.integrity_reason?' ('+r.integrity_reason+')':'')]]));
 out.push(whoCard('Closest submission by another user',o,r.sim_other_id,r.sim_other));
 if(r.sim_own_id)out.push(whoCard('Closest of '+user(r.username)+'’s own earlier submissions',w,r.sim_own_id,r.sim_own));
 if(r.shared&&r.shared.matched_submission_id){const m=byId(r.shared.matched_submission_id);
  out.push(el('h4','Shared text (chunk overlap)'));out.push(kv([['Shared',pc(r.shared.shared)+'%'],['Of this in theirs',pc(r.shared.ours_in_theirs)+'%'],['Of theirs in this',pc(r.shared.theirs_in_ours)+'%'],['Matched',m?user(m.username)+' · '+m.submission:r.shared.matched_submission_id]]))}
 out.push(el('p','Similarity is the site’s embedding similarity (0–1) to the closest submission; shared text is the copy check’s chunk overlap. Both come from the site as published.','note'));
 openModal('Similarity · '+r.submission,user(r.username)+' · UID '+r.uid,out);
}

/* ---------- board ---------- */
function draw(){
 $('asof').replaceChildren();if(ERR)$('asof').appendChild(el('span','Last read failed: '+ERR+' · ','err'));
 if(!D){$('asof').appendChild(document.createTextNode(BUSY?'Reading the Lab…':''));return}
 $('asof').appendChild(document.createTextNode('Data as of '+D.fetched+' UTC'+(BUSY?' · updating…':'')));
 $('rname').textContent='· '+D.round;const L=D.leader;
 $('lede').replaceChildren(document.createTextNode(D.round+' ('+D.title+') is '+(D.open?'open until '+D.closes+' UTC':D.opens>D.fetched?'not open yet: submissions open '+D.opens+' UTC':'closed')+'. '),
  el('b',L?user(L.username)+' (UID '+L.uid+')':'Nobody'),document.createTextNode(L?' leads with ':' leads.'),el('b',L?f3(L.score):''),
  document.createTextNode(L?'; a new entry needs more than '+f3(L.score+D.margin)+' to take the lead. ':' '),el('b',String(D.waiting)),document.createTextNode(' waiting. '));
 const a=el('a','Open on sundaebar.ai');a.href=D.url+'/submissions';a.target='_blank';a.rel='noopener';$('lede').appendChild(a);
 const T=[['Leader',L?user(L.username):'—',L?'UID '+L.uid+' · '+sh(L.hotkey):''],['Leader score',L?f3(L.score):'—','Top score '+f3(D.top)],['To take the lead',L?'> '+f3(L.score+D.margin):'—','Leader + '+f3(D.margin)+' margin'],
  ['Submissions',D.count,D.scored+' scored · '+D.failed+' failed'],['Waiting',D.waiting,'Not scored yet'],['Miners',D.miners,'Distinct hotkeys'],['Closes (UTC)',D.closes||'—',(D.opens>D.fetched?'Opens ':'Opened ')+D.opens]];
 $('tiles').replaceChildren(...T.map(([k,v,s])=>{const t=el('div',null,'t');t.append(el('div',k,'k'),el('div',String(v),'v'),el('div',s,'s'));return t}));
 let R=D.rows.filter(r=>r.status==='scored'&&r.score!=null).sort((a,b)=>a.pos-b.pos);
 if($('best').checked){const s=new Set();R=R.filter(r=>{const k=r.hotkey||r.username;if(s.has(k))return false;s.add(k);return true})}
 const RF=R.filter(match);$('rc').textContent=RF.length+($('best').checked?' miners':' entries');
 fill('rank',RF.map(r=>row([[r.pos,'n'],[user(r.username)],[r.uid,'n'],[hk(r.hotkey)],[lk(f3(r.score),()=>showScore(r),'Score detail for '+r.submission),'n'],[pill(r.integrity||'—',r.integrity==='active'?'ok':r.integrity?'bad':'mut')],[lk(r.submission,()=>showFiles(r),'Download '+r.submission)],[r.submitted],[r.sim_other==null?'—':lk((+r.sim_other).toFixed(3),()=>showSim(r),'Similarity for '+r.submission),'n']],(r.leader?'lead ':'')+(recent(r)?'new':''))),9,'Nothing scored yet.');
 const W=D.rows.filter(r=>r.status!=='scored'&&r.status!=='failed').sort((a,b)=>a.submitted<b.submitted?-1:1);$('wc').textContent=W.length+' waiting';
 fill('wait',W.map((r,i)=>[i+1,r]).filter(([,r])=>match(r)).map(([i,r])=>row([[i,'n'],[user(r.username)],[r.uid,'n'],[hk(r.hotkey)],[pill(r.status,'warn')],[r.submitted]])),6,'Queue is empty.');
 const F=D.rows.filter(r=>r.status==='failed');$('fc').textContent=F.length+' failed';
 fill('fail',F.filter(match).map(r=>row([[user(r.username)],[r.uid,'n'],[hk(r.hotkey)],[lk(r.submission,()=>showFiles(r),'Download '+r.submission)],[r.submitted]])),5,'No failed evaluations.');
 document.title=(L?f3(L.score)+' '+user(L.username)+' · ':'')+'SN121 Live Board';
}
async function load(){try{const s=await (await fetch((STATIC?'data.json?t=':'/data.json?t=')+Date.now(),{cache:'no-store'})).json();D=s.data||D;ERR=s.error;BUSY=s.busy}catch(e){ERR=STATIC?'could not load data.json':'board server not running'}if(!$('dlg').open)draw()}
$('best').onchange=draw;$('q').oninput=draw;$('dlg').addEventListener('close',draw);
// ---- GitHub Pages: the button starts the workflow through GitHub's API with the owner's token (kept only in this browser)
const GH=(()=>{const m=location.hostname.match(/^([^.]+)\.github\.io$/i);if(!m)return null;const r=location.pathname.split('/').filter(Boolean)[0];return {o:m[1],r:r||location.hostname}})();
const TK='sn121_gh_token',sleep=ms=>new Promise(r=>setTimeout(r,ms)),mmss=ms=>{const s=Math.max(0,Math.round(ms/1000));return Math.floor(s/60)+':'+String(s%60).padStart(2,'0')};
function tok(){try{return localStorage.getItem(TK)||''}catch(e){return ''}}
function setTok(v){try{v?localStorage.setItem(TK,v):localStorage.removeItem(TK)}catch(e){}$('gtk').hidden=!v}
function gst(t,cls,url){const s=$('gst');s.className='gst'+(cls?' '+cls:'');s.replaceChildren(document.createTextNode(t));if(url){const a=el('a','see run');a.href=url;a.target='_blank';a.rel='noopener';s.appendChild(a)}}
async function gh(path,opt={}){const r=await fetch('https://api.github.com/repos/'+GH.o+'/'+GH.r+path,{...opt,cache:'no-store',headers:{Accept:'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28',Authorization:'Bearer '+tok(),...(opt.body?{'Content-Type':'application/json'}:{})}});
 if(r.status===401){setTok('');throw new Error('GitHub rejected the token; enter a new one')}
 if(!r.ok)throw new Error('GitHub '+r.status+(r.status===403||r.status===404?': the token needs Actions "Read and write" on '+GH.r:''));return r.status===204?null:r.json()}
let WF=null,TRACK=false;
async function wf(){if(WF)return WF;const w=(await gh('/actions/workflows')).workflows.filter(w=>w.state==='active');
 WF=w.find(w=>/\/(main|board)\.ya?ml$/.test(w.path))||w.find(w=>/board/i.test(w.name))||w[0];if(!WF)throw new Error('no active workflow in '+GH.r);return WF}
async function runs(){return (await gh('/actions/workflows/'+(await wf()).id+'/runs?per_page=10')).workflow_runs}
async function step(run){try{const j=(await gh('/actions/runs/'+run.id+'/jobs')).jobs[0];const s=j&&j.steps&&j.steps.find(s=>s.status==='in_progress');return s?s.name.replace(/^Run /,''):''}catch(e){return ''}}
async function track(run){if(TRACK)return;TRACK=true;$('now').disabled=true;const t0=Date.parse(run.created_at),before=D&&D.fetched;
 try{while(run.status!=='completed'){gst(run.status==='queued'||run.status==='waiting'?'Update queued on GitHub… '+mmss(Date.now()-t0):'Updating: '+((await step(run))||'running')+'… '+mmss(Date.now()-t0));await sleep(5000);run=await gh('/actions/runs/'+run.id)}
  if(run.conclusion!=='success'){gst('Update '+run.conclusion+'. ','err',run.html_url);return}
  gst('Published, loading the new data…');for(let i=0;i<24;i++){await load();if(D&&D.fetched!==before)break;await sleep(5000)}
  gst(D&&D.fetched!==before?'Updated in '+mmss(Date.now()-t0)+'.':'Published; the site may take a minute to show it.','ok')}
 catch(e){gst(e.message,'err')}finally{TRACK=false;$('now').disabled=false}}
async function refreshNow(){if(!tok())return askTok();if(TRACK)return;$('now').disabled=true;gst('Starting an update…');
 try{const rs=await runs();let run=rs.find(r=>r.status!=='completed');
  if(!run){const seen=new Set(rs.map(r=>r.id)),repo=await gh('');await gh('/actions/workflows/'+(await wf()).id+'/dispatches',{method:'POST',body:JSON.stringify({ref:repo.default_branch})});
   for(let i=0;i<20&&!run;i++){await sleep(3000);run=(await runs()).find(r=>!seen.has(r.id))}if(!run)throw new Error('GitHub accepted the request but no run appeared yet; try again in a minute')}
  $('now').disabled=false;await track(run)}
 catch(e){$('now').disabled=false;gst(e.message,'err');if(!tok())askTok()}}
function askTok(){const inp=el('input');inp.type='password';inp.autocomplete='off';inp.placeholder='github_pat_…';inp.setAttribute('aria-label','GitHub token');
 const ol=el('ol');const nt='https://github.com/settings/personal-access-tokens/new';
 [['On GitHub open ',['Settings → Developer settings → Fine-grained tokens → Generate new token',nt]],['Repository access: ',null,'Only select repositories → '+GH.r],['Permissions → Repository permissions → ',null,'Actions: Read and write (nothing else)'],['Generate it and paste it below.']].forEach(([a,l,b])=>{const li=el('li',a);if(l){const x=el('a',l[0]);x.href=l[1];x.target='_blank';x.rel='noopener';li.appendChild(x)}if(b)li.appendChild(el('b',b));ol.appendChild(li)});
 const save=el('button','Save and update','b pri');save.type='button';save.onclick=()=>{const v=inp.value.trim();if(!v){inp.focus();return}setTok(v);$('dlg').close();refreshNow()};
 inp.onkeydown=e=>{if(e.key==='Enter')save.click()};const bt=el('div',null,'row');bt.appendChild(save);
 if(tok()){const f=el('button','Forget saved token','b');f.type='button';f.onclick=()=>{setTok('');$('dlg').close();gst('Token removed from this browser.')};bt.appendChild(f)}
 const box=el('div',null,'tk');box.append(el('p','The button asks GitHub to run this site\'s update workflow (about 2 minutes). That needs a GitHub token from the repository owner. It is kept only in this browser and sent only to api.github.com.'),ol,inp,bt);
 openModal('Connect GitHub','One-time setup for '+GH.o+'/'+GH.r,[box]);setTimeout(()=>inp.focus(),50)}
if(STATIC){if(GH){$('now').textContent='Refresh now';$('now').onclick=refreshNow;$('gtk').onclick=askTok;$('gtk').hidden=!tok();if(tok())runs().then(rs=>{const r=rs.find(r=>r.status!=='completed');if(r)track(r)}).catch(()=>{})}else $('now').hidden=true}else{$('now').onclick=async()=>{await fetch('/refresh');BUSY=true;draw();setTimeout(load,1500)}}
load();setInterval(load,STATIC?60000:20000);
</script></body></html>"""


# ---------------------------------------------------------------- entry points

def once(args):
    os.makedirs(args.once, exist_ok=True)
    with open(os.path.join(args.once, "index.html"), "w", encoding="utf-8") as f:
        f.write(PAGE.replace("const STATIC=false;", "const STATIC=true;"))
    if args.prev:
        try:
            with urllib.request.urlopen(args.prev + ("&" if "?" in args.prev else "?") + "t=" + str(int(time.time())), timeout=30) as r:
                DETAILS.update(json.load(r))
            print(f"reused {len(DETAILS)} details from the last update", flush=True)
        except Exception as e:
            print(f"no earlier details at {args.prev} ({type(e).__name__}); reading them all", flush=True)
    err, d = None, None
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_page(viewport={"width": 1400, "height": 1800})
        for attempt in range(3):
            try:
                d = scrape(pg, args.round)
                break
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                print("read failed:", err, file=sys.stderr, flush=True)
                time.sleep(10)
        if d:
            n = fetch_details(pg, d["url"], d["rows"], limit=args.details_max, budget_s=150)
            left = sum(r["status"] in DONE and r["id"] not in DETAILS for r in d["rows"])
            print(f"details: +{n} read{f', {left} left for the next update' if left else ''}", flush=True)
        b.close()
    keep = {r["id"] for r in d["rows"]} if d else set(DETAILS)
    with open(os.path.join(args.once, "details.json"), "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in DETAILS.items() if k in keep}, f)
    with open(os.path.join(args.once, "data.json"), "w", encoding="utf-8") as f:
        json.dump({"data": d, "error": None if d else err, "busy": False}, f)
    if d:
        print(f"[{d['fetched']} UTC] {d['round']}: {d['count']} submissions, {d['waiting']} waiting", flush=True)
    return 0 if d else 1


def main():
    ap = argparse.ArgumentParser(description="Live board for the sundae_bar Lab")
    ap.add_argument("--round", type=int, default=None, help="round number, e.g. 40 (default: the live round)")
    ap.add_argument("--every", type=int, default=60, help="seconds between reads of the Lab (default 60, minimum 20)")
    ap.add_argument("--port", type=int, default=8121)
    ap.add_argument("--once", metavar="DIR", help="read the Lab once, write DIR/index.html, data.json and details.json, then exit")
    ap.add_argument("--prev", metavar="URL", help="with --once: the published details.json to reuse, so only new submissions are opened")
    ap.add_argument("--details-max", type=int, default=20, help="with --once: most submissions to read grades for per run (default 20; the site rate-limits)")
    args = ap.parse_args()
    if args.once:
        return once(args)
    args.every = max(20, args.every)
    wake = threading.Event()
    H.wake = wake
    threading.Thread(target=worker, args=(args, wake), daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), H)
    print(f"Board: http://127.0.0.1:{args.port}  (reads the Lab every {args.every}s; Ctrl+C to stop)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main() or 0)
