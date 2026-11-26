import json,csv,datetime
d=json.load(open('subs.json'))
ch={c['version']:c for c in d['_challenges']['challenges']}
DONE={'scored','failed'}
import os
FETCHED=datetime.datetime.utcfromtimestamp(os.path.getmtime('subs.json')).strftime('%Y-%m-%d %H:%M')
subs_rows=[];round_rows=[]
for n in sorted(k for k in d if k.startswith('SBC')):
    v=int(n[3:]); r=d[n]; c=ch.get(v,{}); m=r['meta'] or {}
    S=r['subs']
    lead_id=m.get('current_leader_submission_id')
    ranked=sorted([s for s in S if s['validation_status']=='scored' and s['total_score'] is not None],key=lambda s:-s['total_score'])
    if not lead_id and ranked and c.get('status')!='active':
        act=[s for s in ranked if s.get('integrity_status')=='active']
        lead_id=(act or ranked)[0]['id']
    pos={s['id']:i+1 for i,s in enumerate(ranked)}
    for s in S:
        files=(s.get('data') or {}).get('skill_files') or []
        subs_rows.append(dict(round=n,pos=pos.get(s['id'],''),submission=s['display_name'],username=s.get('username') or '',
          miner_uid=s.get('miner_uid') if s.get('miner_uid') is not None else '',hotkey=s.get('miner_hotkey') or '',
          score=round(s['total_score']*100,3) if s['total_score'] is not None else '',status=s['validation_status'],
          integrity=s.get('integrity_status') or '',leader='yes' if s['id']==lead_id else '',
          submitted=s['created_at'][:19].replace('T',' '),updated=(s.get('updated_at') or '')[:19].replace('T',' '),
          sim_own=s.get('similarity_score_own') or '',sim_other=s.get('similarity_score_other') or '',
          files=len(files),bytes=sum(f.get('size') or 0 for f in files),id=s['id']))
    L=next((s for s in S if s['id']==lead_id),None)
    waiting=[s for s in S if s['validation_status'] not in DONE]
    round_rows.append(dict(round=n,title=c.get('title',''),state=('open' if c.get('submissions_open') or c.get('status')=='active' else 'closed'),
      opens=(c.get('submissions_open_at') or '')[:16].replace('T',' '),closes=(c.get('submissions_close_at') or '')[:16].replace('T',' '),
      submissions=m.get('count',len(S)),scored=sum(s['validation_status']=='scored' for s in S),failed=sum(s['validation_status']=='failed' for s in S),
      waiting=len(waiting),miners=len({s.get('miner_hotkey') for s in S}),top_score=round(m['top_score']*100,3) if m.get('top_score') else '',
      leader_user=L and L.get('username') or '',leader_uid=L['miner_uid'] if L else '',leader_hotkey=L and L.get('miner_hotkey') or '',
      leader_score=round(L['total_score']*100,3) if L and L['total_score'] is not None else '',leader_submission=L and L['display_name'] or '',
      margin=round((m.get('leadership_margin') or 0)*100,3),fetched=FETCHED))
for name,rows in [('submissions.csv',subs_rows),('rounds.csv',round_rows)]:
    w=csv.DictWriter(open(name,'w',newline=''),fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
print(len(subs_rows)); [print(r) for r in round_rows]
print({c['version']:(c.get('status'),c.get('submissions_open')) for c in ch.values() if c['version']>=30})
