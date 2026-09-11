from pathlib import Path
import json, urllib.request, urllib.parse

DEFAULT={"enabled":False,"provider":"ollama","endpoint":"http://127.0.0.1:11434","model":"","use_for_watch_compile":True,"fallback_to_primary":True,"timeout_seconds":45}
ALLOWED_SOURCES={"pubmed","europe_pmc","arxiv","biorxiv","medrxiv"}
ALLOWED_FREQ={"daily","weekly","biweekly","monthly"}

class LocalLLM:
    def __init__(self,path): self.path=Path(path); self.config=self._read()
    def _read(self):
        x=dict(DEFAULT)
        try:
            if self.path.exists(): x.update(json.loads(self.path.read_text(encoding="utf-8")))
        except Exception: pass
        return x
    def public_config(self): return dict(self.config)
    def save_config(self,data):
        x=dict(self.config); x.update({k:data[k] for k in DEFAULT if k in data})
        if x['provider'] not in ('ollama','openai_compatible'): raise ValueError('provider must be ollama or openai_compatible')
        x['endpoint']=str(x.get('endpoint') or '').rstrip('/'); x['model']=str(x.get('model') or '').strip(); x['timeout_seconds']=max(5,min(180,int(x.get('timeout_seconds') or 45)))
        self.path.parent.mkdir(parents=True,exist_ok=True); self.path.write_text(json.dumps(x,ensure_ascii=False,indent=2),encoding='utf-8'); self.config=x; return x
    def health(self, ignore_enabled=False, probe_generate=False):
        if not ignore_enabled and not self.config.get('enabled'):
            return {'ok':False,'disabled':True,'model':self.config.get('model','')}
        try:
            if self.config['provider']=='ollama':
                data=self._request('/api/tags',None,'GET'); names=[m.get('name','') for m in data.get('models',[])]; model=self.config.get('model','')
                if not model:
                    return {'ok':False,'reachable':True,'model':'','available_models':names,'error':'No local model configured'}
                if model not in names:
                    return {'ok':False,'reachable':True,'model':model,'available_models':names,'error':'Configured model not found'}
                out={'ok':True,'reachable':True,'model':model,'available_models':names,'error':''}
                if probe_generate:
                    x=self._request('/api/generate',{'model':model,'prompt':'Return exactly: OK','stream':False,'options':{'temperature':0,'num_predict':8}})
                    out['probe_response']=str(x.get('response') or '').strip()[:120]
                return out
            return {'ok':True,'model':self.config.get('model',''),'note':'OpenAI-compatible endpoint is validated on first request'}
        except Exception as e:return {'ok':False,'model':self.config.get('model',''),'error':f'{type(e).__name__}: {e}'}
    def _request(self,path,payload=None,method='POST'):
        url=self.config['endpoint']+path; data=None if payload is None else json.dumps(payload).encode('utf-8'); req=urllib.request.Request(url,data=data,method=method,headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=self.config['timeout_seconds']) as r:return json.loads(r.read().decode('utf-8'))
    def generate_json(self,prompt):
        model=self.config.get('model','')
        if not model: raise ValueError('Local model is not configured')
        if self.config['provider']=='ollama':
            x=self._request('/api/generate',{'model':model,'prompt':prompt,'stream':False,'format':'json','options':{'temperature':0}}); return str(x.get('response') or '').strip()
        x=self._request('/v1/chat/completions',{'model':model,'temperature':0,'response_format':{'type':'json_object'},'messages':[{'role':'user','content':prompt}]}); return str(x['choices'][0]['message']['content']).strip()

def validate_watch_proposal(p,current,profile):
    if not isinstance(p,dict): raise ValueError('proposal must be a JSON object')
    out={}
    watch=p.get('watch')
    if watch is not None:
        if not isinstance(watch,dict): raise ValueError('watch must be an object')
        clean={}
        for cat in ('research','methods','review'):
            if cat not in watch: continue
            v=watch[cat]
            if not isinstance(v,dict): raise ValueError(f'watch.{cat} must be an object')
            q=dict(v)
            if 'frequency' in q and q['frequency'] not in ALLOWED_FREQ: raise ValueError(f'invalid frequency for {cat}')
            if 'threshold' in q: q['threshold']=max(0,min(100,int(q['threshold'])))
            if 'max_candidates' in q: q['max_candidates']=max(1,min(100,int(q['max_candidates'])))
            if 'sources' in q:
                if not isinstance(q['sources'],list): raise ValueError('sources must be a list')
                q['sources']=[str(s) for s in q['sources'] if str(s) in ALLOWED_SOURCES]
            if 'query' in q: q['query']=str(q['query'])[:2000]
            clean[cat]=q
        out['watch']=clean
    if 'limits' in p:
        v=p['limits'] if isinstance(p['limits'],dict) else {}; out['limits']={}
        if 'max_source_requests_per_day' in v: out['limits']['max_source_requests_per_day']=max(1,min(500,int(v['max_source_requests_per_day'])))
        if 'max_llm_screenings_per_day' in v: out['limits']['max_llm_screenings_per_day']=max(1,min(100,int(v['max_llm_screenings_per_day'])))
    if 'inbox_retention_days' in p: out['inbox_retention_days']=max(0,min(3650,int(p['inbox_retention_days'])))
    if 'profile' in p and isinstance(p['profile'],dict): out['profile']={k:str(v)[:5000] for k,v in p['profile'].items()}
    if not out: raise ValueError('proposal contains no supported configuration changes')
    return out
