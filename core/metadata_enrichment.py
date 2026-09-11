import difflib
import html
import json
import re
import urllib.parse
import urllib.request

UA = 'Literature-Agent/1.0.2 metadata enrichment (personal research tool)'

def _get_json(url, timeout=20):
    req=urllib.request.Request(url,headers={'User-Agent':UA,'Accept':'application/json'})
    with urllib.request.urlopen(req,timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8','replace'))

def _norm(s):
    return re.sub(r'[^a-z0-9]+',' ',str(s or '').lower()).strip()

def _similar(a,b):
    a=_norm(a); b=_norm(b)
    return difflib.SequenceMatcher(None,a,b).ratio() if a and b else 0.0

def _strip_markup(s):
    s=re.sub(r'<[^>]+>',' ',str(s or ''))
    return re.sub(r'\s+',' ',html.unescape(s)).strip()

def _openalex_abstract(inv):
    if not isinstance(inv,dict): return ''
    pairs=[]
    for word,positions in inv.items():
        for p in positions or []: pairs.append((int(p),word))
    pairs.sort()
    return ' '.join(w for _,w in pairs).strip()

def _europe_pmc(title, doi=''):
    q=('DOI:'+doi) if doi else ('TITLE:"'+title.replace('"','')+'"')
    url='https://www.ebi.ac.uk/europepmc/webservices/rest/search?'+urllib.parse.urlencode({'query':q,'format':'json','pageSize':5,'resultType':'core'})
    data=_get_json(url)
    best=None; score=0
    for r in data.get('resultList',{}).get('result',[]):
        s=_similar(title,r.get('title',''))
        if s>score: best,score=r,s
    if not best or score<0.88: return None
    return {'source':'Europe PMC','match':score,'doi':best.get('doi','') or '', 'abstract':_strip_markup(best.get('abstractText','') or ''), 'pmid':best.get('pmid','') or '', 'pmcid':best.get('pmcid','') or ''}

def _openalex(title, doi=''):
    if doi:
        url='https://api.openalex.org/works/https://doi.org/'+urllib.parse.quote(doi,safe='')
        try: works=[_get_json(url)]
        except Exception: works=[]
    else:
        url='https://api.openalex.org/works?'+urllib.parse.urlencode({'search':title,'per-page':5})
        works=_get_json(url).get('results',[])
    best=None; score=0
    for r in works:
        s=_similar(title,r.get('title',''))
        if s>score: best,score=r,s
    if not best or score<0.88: return None
    doi2=str(best.get('doi') or '').replace('https://doi.org/','')
    return {'source':'OpenAlex','match':score,'doi':doi2,'abstract':_openalex_abstract(best.get('abstract_inverted_index')), 'pmid':'','pmcid':''}

def enrich_metadata(title, doi=''):
    """Deterministic metadata enrichment. No LLM and no PDF/full-text access."""
    attempts=[]
    for fn in (_europe_pmc,_openalex):
        try:
            x=fn(title,doi)
            if x:
                attempts.append(x)
                if x.get('abstract'): return x
        except Exception:
            continue
    return attempts[0] if attempts else None
