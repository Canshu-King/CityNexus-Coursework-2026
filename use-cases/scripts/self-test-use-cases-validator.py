from pathlib import Path
import subprocess, tempfile, shutil, yaml, json
ROOT=Path(__file__).resolve().parents[1]; PROJECT=ROOT.parent
CASES=[]
def setup(p):
    (p/'use-cases'/'scripts').mkdir(parents=True); (p/'use-cases'/'qa').mkdir(parents=True); (p/'requirements').mkdir(parents=True); (p/'domain-model').mkdir(parents=True)
    for src,dst in [(ROOT/'use-cases.yaml',p/'use-cases'/'use-cases.yaml'),(ROOT/'use-cases.schema.json',p/'use-cases'/'use-cases.schema.json'),(ROOT/'scripts'/'validate-use-cases.py',p/'use-cases'/'scripts'/'validate-use-cases.py'),(PROJECT/'requirements'/'requirements.yaml',p/'requirements'/'requirements.yaml'),(PROJECT/'domain-model'/'citynexus-core-domain-model-v0.4.2.yaml',p/'domain-model'/'citynexus-core-domain-model-v0.4.2.yaml')]: shutil.copy2(src,dst)
def run_case(name,mutate,raw=False):
    with tempfile.TemporaryDirectory() as td:
        p=Path(td)/'proj'; setup(p); f=p/'use-cases'/'use-cases.yaml'
        if raw: mutate(None,f)
        else:
            d=yaml.safe_load(f.read_text(encoding='utf-8')); mutate(d,f); f.write_text(yaml.safe_dump(d,sort_keys=False,allow_unicode=True),encoding='utf-8')
        cp=subprocess.run(['python',str(p/'use-cases'/'scripts'/'validate-use-cases.py')],cwd=p/'use-cases',stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10)
        CASES.append({'name':name,'pass':cp.returncode!=0,'returnCode':cp.returncode})
def dup(_,f): f.write_text(f.read_text(encoding='utf-8')+'\nmetadata:\n  name: duplicate\n',encoding='utf-8')
run_case('duplicate YAML key rejected',dup,True)
run_case('missing top-level UC rejected',lambda d,f:d['useCases'].pop())
run_case('wrong domain kind rejected',lambda d,f:d['useCases'][0]['domainElements'][0].__setitem__('kind','enum'))
run_case('bad relationship endpoint rejected',lambda d,f:d['relationships'][0].__setitem__('target','UC-99'))
out={'total':len(CASES),'passed':sum(c['pass'] for c in CASES),'failed':sum(not c['pass'] for c in CASES),'cases':CASES}; (ROOT/'qa'/'use-case-validator-self-test.json').write_text(json.dumps(out,indent=2),encoding='utf-8'); print(f"Use Case Self-Test: {out['passed']} PASS / {out['failed']} FAIL"); raise SystemExit(1 if out['failed'] else 0)
