import json,sys,re,time
from playwright.sync_api import sync_playwright
slugs=[l.split() for l in open(sys.argv[2] if len(sys.argv)>2 else "slugs.txt") if l.strip()]
out=json.load(open("subs.json")) if len(sys.argv)>1 and sys.argv[1]=="resume" else {}
with sync_playwright() as p:
    b=p.chromium.launch(); pg=b.new_page(viewport={"width":1500,"height":2200})
    got={}
    def on(r):
        if 'api.sundaebar.ai/api/v2/submissions?' in r.url and 'limit=10' in r.url:
            try: got[r.url]=r.json()
            except Exception as e: print("err",e,file=sys.stderr)
        if 'api/v2/challenges?limit=100' in r.url:
            try: got['challenges']=r.json()
            except: pass
    pg.on("response",on)
    for n,slug in slugs:
        if n in out: continue
        got.clear()
        pg.goto(f"https://www.sundaebar.ai/lab/challenges/{slug}/submissions",wait_until="networkidle",timeout=90000); pg.wait_for_timeout(1500)
        for i in range(200):
            pages=[k for k in got if k.startswith('http')]
            nxt=pg.locator('button[aria-label="Go to next page"]').first
            try:
                dis=nxt.is_disabled()
            except Exception: dis=True
            if dis: break
            before=len(pages); nxt.click()
            for _ in range(40):
                pg.wait_for_timeout(250)
                if len([k for k in got if k.startswith('http')])>before: break
            else: break
            pg.wait_for_timeout(300)
        subs=[]; meta=None
        for k,v in got.items():
            if k.startswith('http'):
                subs+=v.get('submissions',[]); meta={x:v.get(x) for x in v if x!='submissions'}
        out[n]={"slug":slug,"subs":subs,"meta":meta}
        if 'challenges' in got: out['_challenges']=got['challenges']
        print(n,len(subs),meta and meta.get('count'),file=sys.stderr)
        json.dump(out,open("subs.json","w"))
    b.close()
