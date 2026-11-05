"""Real API smoke flow; run only against your local, disposable development instance.

Usage:
  TEST_WEBSITE=https://example.com python scripts/e2e_smoke.py
  TEST_WEBSITE=https://your-disposable-test-site.com TEST_APPROVE=1 python scripts/e2e_smoke.py

TEST_APPROVE=1 explicitly approves required fields under the disposable QA account; never use on real accounts.
No token/password is written to the JSON output.
"""
import asyncio
import json
import os
import secrets
import time
import uuid
from pathlib import Path
import httpx

BASE = os.environ.get('TEST_API', 'http://127.0.0.1:8000').rstrip('/')
WEBSITE = os.environ.get('TEST_WEBSITE', 'https://example.com')
APPROVE = os.environ.get('TEST_APPROVE') == '1'


async def run():
    report={'base_url':BASE,'website':WEBSITE,'calls':[],'overall':'BLOCKED','blockers':[]}
    async with httpx.AsyncClient(timeout=40) as client:
        token=None
        async def call(method,path,payload=None,form=None):
            headers={'Authorization':'Bearer '+token} if token else {}
            if form:
                resp=await client.request(method,BASE+path,headers=headers,data=form)
            else:
                resp=await client.request(method,BASE+path,headers=headers,json=payload)
            try:body=resp.json()
            except ValueError:body={'raw_response':resp.text[:300]}
            report['calls'].append({'method':method,'path':path,'status':resp.status_code,
                                    'request':payload if method!='POST' or path!='/auth/signup' else '[REDACTED]',
                                    'response':{'keys':list(body.keys()),
                                                'error':body.get('error') if isinstance(body,dict) else None}})
            return resp,body
        try:
            _,health=await call('GET','/health')
            if health.get('status')!='ok':raise RuntimeError('Health failed')
            email='coirei-qa-'+uuid.uuid4().hex[:10]+'@example.com'
            resp,user=await call('POST','/auth/signup', {'email':email,'name':'Coirei QA',
                       'password':secrets.token_urlsafe(25),'company_name':'MVP QA'})
            assert resp.status_code==201,user
            token=user['data']['access_token']; cid=user['data']['company_id']
            resp,ctx=await call('POST',f'/companies/{cid}/context', form={'website_url':WEBSITE,
                                  'prompt':'Extract only attributable products, pricing and ICP.'})
            assert resp.status_code==202,ctx
            job_id=ctx['data']['job_id']
            stage='queued';status={}
            for _ in range(90):
                await asyncio.sleep(2)
                resp,status=await call('GET',f'/companies/{cid}/status')
                state=status['data']['status']
                if state in ('completed','failed'):
                    stage=state;break
            else:report['blockers'].append('Job did not finish within bounded polling')
            for path in [f'/companies/{cid}/profile',f'/companies/{cid}/sources',
                         f'/companies/{cid}/conversation',f'/companies/{cid}/products']:
                await call('GET',path)
            _,out=await call('GET',f'/companies/{cid}/products')
            ids=[p['id'] for p in out['data']['products']]
            for pid in ids:await call('GET',f'/products/{pid}')
            await call('POST',f'/companies/{cid}/gap-questions')
            if ids and APPROVE:
                for pid in ids:
                    _,d=await call('GET',f'/products/{pid}')
                    p=d['data']
                    if p['candidate_kind']!='standalone_product':
                        report['blockers'].append(f'Product {pid} not standalone; not overriding classification')
                        continue
                    if any(not p.get(k) for k in ('description','key_features','main_use_case')):
                        report['blockers'].append(f'Product {pid} missing required source facts; not inventing')
                        continue
                    revision=p['revision']
                    _,edit=await call('PATCH',f'/products/{pid}', {'revision':revision,
                          'accept_fields':['description','key_features','main_use_case']})
                    revision=edit['data']['product']['revision']
                    await call('POST',f'/products/{pid}/verify',{'revision':revision,'status':'verified'})
                await call('POST',f'/companies/{cid}/complete')
            elif ids:
                report['blockers'].append('Approval disabled by default; rerun with TEST_APPROVE=1 in isolated environment')
            _,chat=await call('POST',f'/companies/{cid}/messages',{
                'message':'What products did you find?', 'client_message_id':str(uuid.uuid4())})
            await call('GET',f'/companies/{cid}/conversation')
            if stage!='completed':report['blockers'].append(f'Job status: {stage}')
            if not ids:report['blockers'].append('No product candidates discovered')
            report['company_id']=cid
            report['job_id']=job_id
            report['candidate_product_ids']=ids
            report['overall']='PASS' if not report['blockers'] else 'BLOCKED'
        except Exception as exc:
            report['overall']='FAILED'
            report['blockers'].append(str(exc)[:450])
    dest=Path('reports/mvp-e2e-smoke.json');dest.parent.mkdir(exist_ok=True)
    dest.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(f'Wrote {dest} | {report["overall"]} | {len(report["calls"])} HTTP calls')


if __name__=='__main__':asyncio.run(run())
