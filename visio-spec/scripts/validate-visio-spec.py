from pathlib import Path
import yaml, json, sys
ROOT=Path(__file__).resolve().parents[1]
def load(p): return yaml.safe_load(Path(p).read_text(encoding='utf-8'))
def main():
    checks=[]
    def ck(n,o,d=''): checks.append({'name':n,'status':'PASS' if o else 'FAIL','detail':d})
    meta=load(ROOT/'visio-model-spec.yaml')['metadata']; pages=[]
    for p in sorted((ROOT/'pages').glob('*.yaml')): pages.append(load(p))
    ck('exact seven pages',len(pages)==7,str([p['pageName'] for p in pages]))
    ck('page names match master list',set(p['pageName'] for p in pages)==set(meta['pages']))
    idsbad=[]; refbad=[]; depmult=[]
    for p in pages:
        ids=[s['id'] for s in p.get('shapes',[])]; sid=set(ids)
        if len(ids)!=len(sid): idsbad.append(p['pageName'])
        for c in p.get('connectors',[]):
            if c['source'] not in sid or c['target'] not in sid: refbad.append((p['pageName'],c['id']))
            if c['type']=='dependency' and ('sourceMultiplicity' in c or 'targetMultiplicity' in c): depmult.append((p['pageName'],c['id']))
    ck('shape ids unique per page',not idsbad,str(idsbad)); ck('connector endpoints resolve',not refbad,str(refbad)); ck('dependencies have no multiplicity',not depmult,str(depmult))
    p1=next(p for p in pages if p['pageName']=='P1_UC_Overview'); uc={s['id'] for s in p1['shapes'] if s['type']=='uml_use_case'}; ck('P1 intended use cases exact',uc=={'UC-02','UC-04','UC-05','UC-06','UC-07','UC-08','UC-09','UC-10','UC-11','UC-12'},str(uc))
    p2=next(p for p in pages if p['pageName']=='P2_UC_Core'); uc2={s['id'] for s in p2['shapes'] if s['type']=='uml_use_case'}; ck('P2 top-level use cases exact',uc2=={f'UC-{i:02d}' for i in range(1,14)},str(uc2))
    shape_texts=[str(s.get('text','')).strip() for p in pages for s in p.get('shapes',[])]; ck('obsolete class names absent',all(x not in shape_texts for x in ['TravelerPreference','JourneyPreference','AIInsight','Transaction']))
    jp=next(p for p in pages if p['pageName']=='P2_DM_Journey_Planning'); ck('JourneyTwin projection present',any(s.get('id')=='JourneyTwin' and s.get('stereotype')=='<<projection>>' for s in jp['shapes'])); ck('ScoreBreakdown value object present',any(s.get('id')=='ScoreBreakdown' and s.get('stereotype')=='<<value object>>' for s in jp['shapes']))
    out={'passed':sum(c['status']=='PASS' for c in checks),'failed':sum(c['status']=='FAIL' for c in checks),'checks':checks}; (ROOT/'qa-report.json').write_text(json.dumps(out,indent=2)); print(f"Visio Spec Validator: {out['passed']} PASS / {out['failed']} FAIL"); [print(f"[{c['status']}] {c['name']}") for c in checks]; return 1 if out['failed'] else 0
if __name__=='__main__': raise SystemExit(main())
