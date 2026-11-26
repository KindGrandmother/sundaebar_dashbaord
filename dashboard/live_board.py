"""Local live board for the sundae_bar Lab (SN121).

Reads the live round's public submissions page in a hidden browser every --every
seconds and serves a dashboard at http://127.0.0.1:8121. Reload the page, or wait
for its own 20-second update, to see the latest data.

    python live_board.py                 # live round, refresh every 60 s
    python live_board.py --round 40      # a specific round
    python live_board.py --every 30 --port 8121
    python live_board.py --once site     # one read, writes site/index.html + site/data.json (GitHub Pages)

Needs the same venv as submit_skill.py (Playwright + `playwright install chromium`).
"""
import argparse, json, sys, threading, time, datetime, traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from playwright.sync_api import sync_playwright

SITE = "https://www.sundaebar.ai"
DONE = {"scored", "failed"}
STATE = {"data": None, "error": None, "busy": False}
LOCK = threading.Lock()


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


def summarise(v, url, S, m, c):
    lead_id = m.get("current_leader_submission_id")
    scored = sorted([s for s in S if s["validation_status"] == "scored" and s.get("total_score") is not None],
                    key=lambda s: -s["total_score"])
    pos = {s["id"]: i + 1 for i, s in enumerate(scored)}
    pct = lambda x: None if x is None else round(x * 100, 3)
    rows = []
    for s in S:
        files = (s.get("data") or {}).get("skill_files") or []
        rows.append(dict(
            id=s["id"], pos=pos.get(s["id"]), submission=s.get("display_name"), username=s.get("username") or "",
            uid=s.get("miner_uid"), hotkey=s.get("miner_hotkey") or "", score=pct(s.get("total_score")),
            status=s.get("validation_status"), integrity=s.get("integrity_status") or "",
            leader=s["id"] == lead_id, submitted=(s.get("created_at") or "")[:19].replace("T", " "),
            sim_other=s.get("similarity_score_other"), files=len(files)))
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


def worker(args, wake):
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
                print(f"[{d['fetched']} UTC] {d['round']}: {d['count']} submissions, {d['waiting']} waiting, "
                      f"leader {d['leader'] and d['leader']['username']} ({time.time() - t:.0f}s)", flush=True)
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
        elif path == "/refresh":
            H.wake.set()
            self.send(200, "{}", "application/json")
        elif path in ("/", "/index.html"):
            self.send(200, PAGE, "text/html; charset=utf-8")
        else:
            self.send(404, "not found", "text/plain")


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SN121 Live Board</title>
<style>
:root{--bg:#f6f7f9;--panel:#fff;--fg:#16191f;--mut:#5d6573;--line:#dfe3ea;--ok:#13804b;--warn:#b26a00;--bad:#c2312f;--lead:#e7f5ed;--accent:#2f5bd3}
@media (prefers-color-scheme:dark){:root{--bg:#111318;--panel:#1a1d24;--fg:#e8eaef;--mut:#9aa3b2;--line:#2c313b;--ok:#4cc38a;--warn:#e7a43b;--bad:#f07070;--lead:#173326;--accent:#7d9cf0;color-scheme:dark}}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.w{max-width:1500px;margin:0 auto;padding:20px max(16px,env(safe-area-inset-left))}
h1{font-size:22px;margin:0 0 4px}.lede{color:var(--mut);margin:0 0 14px}.lede b{color:var(--fg)}
.bar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:14px;font-size:13px;color:var(--mut)}
.bar input[type=search]{font:inherit;padding:5px 8px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--fg);width:220px;max-width:100%}
.bar button{font:inherit;padding:5px 12px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--fg);cursor:pointer}
.asof{margin-left:auto}.err{color:var(--bad)}
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
.pill{display:inline-block;padding:0 8px;border-radius:999px;border:1px solid currentColor;font-size:11px;line-height:17px}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.mut{color:var(--mut)}.empty{color:var(--mut);padding:10px}
a{color:var(--accent)}
</style></head><body><div class="w">
<h1>SN121 Live Board <span id="rname"></span></h1>
<p class="lede" id="lede">Loading the Lab… the first read takes about a minute.</p>
<div class="bar"><label><input type="checkbox" id="best"> Best per miner</label>
<input type="search" id="q" placeholder="Filter user, UID or hotkey" aria-label="Filter">
<button id="now" type="button">Read the Lab now</button>
<span class="asof" id="asof"></span></div>
<div class="tiles" id="tiles"></div>
<div class="two"><section class="card"><h2>Ranking <small id="rc"></small></h2><p class="note">Scored entries by score. Green = current leader; blue edge = submitted in the last hour. Click a hotkey to copy it.</p>
<div class="sc"><table><thead><tr><th class="n">#</th><th>User</th><th class="n">UID</th><th>Hotkey</th><th class="n">Score</th><th>Integrity</th><th>Submission</th><th>Submitted (UTC)</th><th class="n">Sim. other</th></tr></thead><tbody id="rank"></tbody></table></div></section>
<div><section class="card"><h2>Waiting <small id="wc"></small></h2><p class="note">Uploaded, not scored yet, oldest first.</p>
<div class="sc s"><table><thead><tr><th class="n">Q</th><th>User</th><th class="n">UID</th><th>Hotkey</th><th>Status</th><th>Submitted (UTC)</th></tr></thead><tbody id="wait"></tbody></table></div></section>
<section class="card"><h2>Failed <small id="fc"></small></h2>
<div class="sc s"><table><thead><tr><th>User</th><th class="n">UID</th><th>Hotkey</th><th>Submission</th><th>Submitted (UTC)</th></tr></thead><tbody id="fail"></tbody></table></div></section></div></div>
</div>
<script>
const STATIC=false;
let D=null,ERR=null,BUSY=false;const $=id=>document.getElementById(id);
const user=u=>u?'@'+u:'(no username)',sh=h=>h&&h.length>14?h.slice(0,6)+'…'+h.slice(-5):(h||''),f3=v=>v==null?'—':(+v).toFixed(3);
function el(t,txt,cls){const e=document.createElement(t);if(txt!=null)e.textContent=txt;if(cls)e.className=cls;return e}
function hk(h){const s=el('span',sh(h),'hk');s.title=h;s.onclick=()=>navigator.clipboard&&navigator.clipboard.writeText(h).then(()=>{s.textContent='copied';setTimeout(()=>s.textContent=sh(h),900)});return s}
function pill(t,c){return el('span',t,'pill '+c)}
function row(cells,cls){const tr=el('tr',null,cls);for(const [v,c] of cells){const td=el('td',null,c);if(v instanceof Node)td.appendChild(v);else td.textContent=v??'';tr.appendChild(td)}return tr}
function fill(id,rows,cols,msg){const tb=$(id);tb.replaceChildren(...(rows.length?rows:[(()=>{const tr=el('tr');const td=el('td',msg,'empty');td.colSpan=cols;tr.appendChild(td);return tr})()]))}
function match(r){const q=$('q').value.trim().toLowerCase();return !q||[r.username,r.uid,r.hotkey,r.submission].some(x=>String(x??'').toLowerCase().includes(q))}
function recent(r){return D&&(Date.parse(D.fetched.replace(' ','T')+'Z')-Date.parse(r.submitted.replace(' ','T')+'Z'))<3600e3}
function draw(){
 $('asof').replaceChildren();if(ERR)$('asof').appendChild(el('span','Last read failed: '+ERR+' · ','err'));
 if(!D){$('asof').appendChild(document.createTextNode(BUSY?'Reading the Lab…':''));return}
 $('asof').appendChild(document.createTextNode('Data as of '+D.fetched+' UTC'+(BUSY?' · updating…':'')));
 $('rname').textContent='· '+D.round;const L=D.leader;
 $('lede').replaceChildren(document.createTextNode(D.round+' ('+D.title+') is '+(D.open?'open until '+D.closes+' UTC':'closed')+'. '),
  el('b',L?user(L.username)+' (UID '+L.uid+')':'Nobody'),document.createTextNode(L?' leads with ':' leads.'),el('b',L?f3(L.score):''),
  document.createTextNode(L?'; a new entry needs more than '+f3(L.score+D.margin)+' to take the lead. ':' '),el('b',String(D.waiting)),document.createTextNode(' waiting. '));
 const a=el('a','Open on sundaebar.ai');a.href=D.url+'/submissions';a.target='_blank';a.rel='noopener';$('lede').appendChild(a);
 const T=[['Leader',L?user(L.username):'—',L?'UID '+L.uid+' · '+sh(L.hotkey):''],['Leader score',L?f3(L.score):'—','Top score '+f3(D.top)],['To take the lead',L?'> '+f3(L.score+D.margin):'—','Leader + '+f3(D.margin)+' margin'],
  ['Submissions',D.count,D.scored+' scored · '+D.failed+' failed'],['Waiting',D.waiting,'Not scored yet'],['Miners',D.miners,'Distinct hotkeys'],['Closes (UTC)',D.closes||'—','Opened '+D.opens]];
 $('tiles').replaceChildren(...T.map(([k,v,s])=>{const t=el('div',null,'t');t.append(el('div',k,'k'),el('div',String(v),'v'),el('div',s,'s'));return t}));
 let R=D.rows.filter(r=>r.status==='scored'&&r.score!=null).sort((a,b)=>a.pos-b.pos);
 if($('best').checked){const s=new Set();R=R.filter(r=>{const k=r.hotkey||r.username;if(s.has(k))return false;s.add(k);return true})}
 const RF=R.filter(match);$('rc').textContent=RF.length+($('best').checked?' miners':' entries');
 fill('rank',RF.map(r=>row([[r.pos,'n'],[user(r.username)],[r.uid,'n'],[hk(r.hotkey)],[f3(r.score),'n'],[pill(r.integrity||'—',r.integrity==='active'?'ok':r.integrity?'bad':'mut')],[r.submission],[r.submitted],[r.sim_other==null?'—':(+r.sim_other).toFixed(3),'n']],(r.leader?'lead ':'')+(recent(r)?'new':''))),9,'Nothing scored yet.');
 const W=D.rows.filter(r=>r.status!=='scored'&&r.status!=='failed').sort((a,b)=>a.submitted<b.submitted?-1:1);$('wc').textContent=W.length+' waiting';
 fill('wait',W.map((r,i)=>[i+1,r]).filter(([,r])=>match(r)).map(([i,r])=>row([[i,'n'],[user(r.username)],[r.uid,'n'],[hk(r.hotkey)],[pill(r.status,'warn')],[r.submitted]])),6,'Queue is empty.');
 const F=D.rows.filter(r=>r.status==='failed');$('fc').textContent=F.length+' failed';
 fill('fail',F.filter(match).map(r=>row([[user(r.username)],[r.uid,'n'],[hk(r.hotkey)],[r.submission],[r.submitted]])),5,'No failed evaluations.');
 document.title=(L?f3(L.score)+' '+user(L.username)+' · ':'')+'SN121 Live Board';
}
async function load(){try{const s=await (await fetch((STATIC?'data.json?t=':'/data.json?t=')+Date.now(),{cache:'no-store'})).json();D=s.data||D;ERR=s.error;BUSY=s.busy}catch(e){ERR=STATIC?'could not load data.json':'board server not running'}draw()}
$('best').onchange=draw;$('q').oninput=draw;
if(STATIC){$('now').hidden=true}else{$('now').onclick=async()=>{await fetch('/refresh');BUSY=true;draw();setTimeout(load,1500)}}
load();setInterval(load,STATIC?60000:20000);
</script></body></html>"""


def once(args):
    import os
    os.makedirs(args.once, exist_ok=True)
    with open(os.path.join(args.once, "index.html"), "w", encoding="utf-8") as f:
        f.write(PAGE.replace("const STATIC=false;", "const STATIC=true;"))
    err = None
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
        else:
            d = None
        b.close()
    out = {"data": d, "error": None if d else err, "busy": False}
    with open(os.path.join(args.once, "data.json"), "w", encoding="utf-8") as f:
        json.dump(out, f)
    if d:
        print(f"[{d['fetched']} UTC] {d['round']}: {d['count']} submissions, {d['waiting']} waiting", flush=True)
    return 0 if d else 1


def main():
    ap = argparse.ArgumentParser(description="Local live board for the sundae_bar Lab")
    ap.add_argument("--round", type=int, default=None, help="round number, e.g. 40 (default: the live round)")
    ap.add_argument("--every", type=int, default=60, help="seconds between reads of the Lab (default 60, minimum 20)")
    ap.add_argument("--port", type=int, default=8121)
    ap.add_argument("--once", metavar="DIR", help="read the Lab once, write DIR/data.json and DIR/index.html, then exit (for GitHub Pages)")
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
