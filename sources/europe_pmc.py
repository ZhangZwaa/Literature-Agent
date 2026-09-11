import json,urllib.parse
from .common import http_text,effective_since

def search(query,max_results=25,policy=None,since=None,category='research'):
    policy=policy or {}; ep=policy.get('europe_pmc') or {}; q=query; dt=effective_since(since,ep.get('date_window_days',0))
    if dt:q+=f' AND FIRST_PDATE:[{dt.strftime("%Y-%m-%d")} TO 3000-12-31]'
    if policy.get('require_open_access'):q+=' AND OPEN_ACCESS:Y'
    if category=='review':q+=' AND (PUB_TYPE:"review" OR PUB_TYPE:"systematic review" OR PUB_TYPE:"meta-analysis")'
    data=json.loads(http_text('https://www.ebi.ac.uk/europepmc/webservices/rest/search?'+urllib.parse.urlencode({'query':q,'format':'json','pageSize':max_results,'sort':'FIRST_PDATE_D desc'}))); out=[]
    for r in data.get('resultList',{}).get('result',[]):
        eid=r.get('pmid') or r.get('pmcid') or r.get('id') or ''; out.append({'source':'europe_pmc','external_id':eid,'title':r.get('title',''),'abstract':r.get('abstractText','') or '','year':str(r.get('pubYear','') or ''),'doi':r.get('doi','') or '','authors':[x.strip() for x in (r.get('authorString','') or '').split(',') if x.strip()][:8],'journal':r.get('journalTitle','') or '','is_open_access':str(r.get('isOpenAccess','')).lower()=='y','url':('https://europepmc.org/article/MED/'+eid if eid else '')})
    return out
