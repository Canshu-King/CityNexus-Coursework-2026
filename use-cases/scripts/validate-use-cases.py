from pathlib import Path
import sys, json, re, yaml
from collections import defaultdict
from jsonschema import Draft202012Validator

ROOT=Path(__file__).resolve().parents[1]
PROJECT=ROOT.parent
MODEL=ROOT/'use-cases.yaml'
SCHEMA=ROOT/'use-cases.schema.json'
REQ=PROJECT/'requirements'/'requirements.yaml'
DOM=PROJECT/'domain-model'/'citynexus-core-domain-model-v0.4.2.yaml'
REPORT=ROOT/'qa'/'use-cases-v0.1-report.json'

class StrictLoader(yaml.SafeLoader): pass
def construct_mapping(loader,node,deep=False):
    m={}
    for k,v in node.value:
        key=loader.construct_object(k,deep=deep)
        if key in m: raise ValueError(f'duplicate YAML key: {key}')
        m[key]=loader.construct_object(v,deep=deep)
    return m
StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,construct_mapping)
def load(p): return yaml.load(Path(p).read_text(encoding='utf-8'),Loader=StrictLoader)

def main():
    checks=[]
    def ck(name,ok,detail=''):
        checks.append({'name':name,'status':'PASS' if ok else 'FAIL','detail':detail})
    try: data=load(MODEL); ck('strict YAML parse',True)
    except Exception as e: data=None; ck('strict YAML parse',False,str(e))
    if data is None:
        out={'passed':sum(x['status']=='PASS' for x in checks),'failed':sum(x['status']=='FAIL' for x in checks),'checks':checks}; REPORT.parent.mkdir(exist_ok=True); REPORT.write_text(json.dumps(out,indent=2)); print(json.dumps(out,indent=2)); return 1
    schema=json.loads(SCHEMA.read_text()); errs=list(Draft202012Validator(schema).iter_errors(data)); ck('JSON Schema validation',not errs,'; '.join(e.message for e in errs[:5]))
    actors={a['id'] for a in data['actors']}; ucs={u['id']:u for u in data['useCases']}; details={s['id'] for u in data['useCases'] for s in u['detailUseCases']}; all_uc_ids=set(ucs)|details
    expected={f'UC-{i:02d}' for i in range(1,14)}; ck('exact top-level UC-01..UC-13',set(ucs)==expected,f'actual={sorted(ucs)}')
    ck('actor ids unique',len(actors)==len(data['actors']))
    bad_actor=[]
    for u in ucs.values():
        for a in [u['primaryActor'],*u['supportingActors']]:
            if a not in actors: bad_actor.append((u['id'],a))
    ck('all actor references resolve',not bad_actor,str(bad_actor))
    req=load(REQ); allreq={}; inbound=defaultdict(set)
    for sec in ['business_goals','functional_requirements','business_rules','non_functional_requirements','security_requirements','integration_requirements']:
        for r in req[sec]:
            allreq[r['id']]=r
            for uid in r.get('useCases',[]): inbound[uid].add(r['id'])
    bad_req=[(u['id'],rid) for u in ucs.values() for rid in u['requirementIds'] if rid not in allreq]; ck('all requirement references resolve',not bad_req,str(bad_req))
    mismatch=[]
    for uid,u in ucs.items():
        if set(u['requirementIds'])!=inbound.get(uid,set()): mismatch.append({'useCase':uid,'missing':sorted(inbound.get(uid,set())-set(u['requirementIds'])),'extra':sorted(set(u['requirementIds'])-inbound.get(uid,set()))})
    ck('requirements mirror baseline useCase mappings',not mismatch,json.dumps(mismatch))
    dom=load(DOM); catalog=set((n,'entity') for n in dom['entities'])|set((n,'value_object') for n in dom['value_objects'])|set((n,'enum') for n in dom['enums'])|set((n,'state_machine') for n in dom['state_machines'])|set((r['id'],'rule') for r in dom['domain_rules'])
    bad_dom=[]
    for u in ucs.values():
        for d in u['domainElements']:
            if (d['name'],d['kind']) not in catalog: bad_dom.append((u['id'],d))
    ck('domain references resolve by name and kind',not bad_dom,str(bad_dom))
    rel_bad=[r for r in data['relationships'] if r['source'] not in all_uc_ids or r['target'] not in all_uc_ids]; ck('relationship endpoints resolve',not rel_bad,str(rel_bad))
    expected_ext={('UC-07','UC-06','[payment required]'),('UC-09','UC-08','[event detected]'),('UC-10','UC-09','[replan required]')}; actual_ext={(r['source'],r['target'],r['condition']) for r in data['relationships'] if r['type']=='extend'}; ck('core extend semantics exact',actual_ext==expected_ext,str(actual_ext))
    expected_details={'UC-04':{'UC-04.1','UC-04.2','UC-04.3','UC-04.4'},'UC-05':{'UC-05.1','UC-05.2','UC-05.3','UC-05.4'},'UC-10':{'UC-10.1','UC-10.2','UC-10.3','UC-10.4','UC-10.5','UC-10.6'}}; detail_m=[]
    for parent,exp in expected_details.items():
        act={s['id'] for s in ucs[parent]['detailUseCases']}
        if act!=exp: detail_m.append((parent,sorted(act),sorted(exp)))
    for uid,u in ucs.items():
        if uid not in expected_details and u['detailUseCases']: detail_m.append((uid,'unexpected detail',u['detailUseCases']))
    ck('focused detail use cases exact',not detail_m,str(detail_m))
    inc={(r['source'],r['target']) for r in data['relationships'] if r['type']=='include'}; missing_inc=[(p,s) for p,subs in expected_details.items() for s in subs if (p,s) not in inc]; ck('detail include relationships complete',not missing_inc,str(missing_inc))
    flow_err=[]
    for u in ucs.values():
        ids=[s['id'] for s in u['mainSuccessScenario']]; expected_ids=[f'MS-{i:02d}' for i in range(1,len(ids)+1)]
        if ids!=expected_ids: flow_err.append((u['id'],ids,expected_ids))
    ck('main-flow step ids sequential and unique',not flow_err,str(flow_err))
    branch_err=[]
    for u in ucs.values():
        for key,pfx in [('alternateFlows','A'),('exceptionFlows','E')]:
            ids=[b['id'] for b in u[key]]
            if len(ids)!=len(set(ids)) or any(not x.startswith(pfx) for x in ids): branch_err.append((u['id'],key,ids))
    ck('alternate and exception flow ids valid',not branch_err,str(branch_err))
    core_actor={'UC-01':'ACT-TRAVELER','UC-02':'ACT-TRAVELER','UC-03':'ACT-TRAVELER','UC-04':'ACT-TRAVELER','UC-05':'ACT-TRAVELER','UC-06':'ACT-TRAVELER','UC-07':'ACT-TRAVELER','UC-08':'ACT-TRAVELER','UC-09':'ACT-CONTEXT','UC-10':'ACT-TRAVELER','UC-11':'ACT-TRAVELER','UC-12':'ACT-TRAVELER','UC-13':'ACT-ADMIN'}
    bad_pa=[(u,ucs[u]['primaryActor'],a) for u,a in core_actor.items() if ucs[u]['primaryActor']!=a]; ck('primary actor baseline semantics',not bad_pa,str(bad_pa))
    no_req=[u['id'] for u in ucs.values() if not u['requirementIds']]; ck('every top-level use case traces requirements',not no_req,str(no_req))
    out={'modelVersion':data['metadata']['version'],'topLevelUseCases':len(ucs),'actors':len(actors),'detailUseCases':len(details),'passed':sum(x['status']=='PASS' for x in checks),'failed':sum(x['status']=='FAIL' for x in checks),'checks':checks}
    REPORT.parent.mkdir(exist_ok=True); REPORT.write_text(json.dumps(out,indent=2),encoding='utf-8')
    print(f"Use Case Validator: {out['passed']} PASS / {out['failed']} FAIL")
    for x in checks: print(f"[{x['status']}] {x['name']}" + (f" :: {x['detail']}" if x['detail'] and x['status']=='FAIL' else ''))
    return 1 if out['failed'] else 0
if __name__=='__main__': raise SystemExit(main())
