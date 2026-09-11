from pathlib import Path
import re,urllib.request,urllib.parse,json,traceback

def safe_filename(name):
    x=re.sub(r'[<>:"/\\|?*]+','_',str(name or 'paper')).strip().rstrip('.')
    return (x[:160] or 'paper')+'.pdf'

def download_urls(meta, urls, download_dir):
    d=Path(download_dir); d.mkdir(parents=True,exist_ok=True); path=d/safe_filename(meta.get('title'))
    if path.exists() and path.stat().st_size>1024:return path,'local-existing'
    for source,url in urls:
        if not url:continue
        try:
            req=urllib.request.Request(url,headers={'User-Agent':'Literature-Agent/0.11 manual PDF recovery'})
            with urllib.request.urlopen(req,timeout=45) as r:ctype=(r.headers.get('Content-Type') or '').lower(); data=r.read(60*1024*1024)
            if data[:4]==b'%PDF' or 'pdf' in ctype:path.write_bytes(data); return path,source
        except Exception:continue
    return None,''

def attach_local_pdf(agent,parent_key,path,log_file=None):
    if not path or not parent_key:return False,'missing path or parent item key'
    path=Path(path).resolve()
    if not path.exists() or path.stat().st_size<1024:return False,f'local PDF is missing or empty: {path}'
    errors=[]
    try:
        if hasattr(agent.zot,'attachment_simple'):
            agent.zot.attachment_simple([str(path)],parentid=parent_key); return True,'imported with attachment_simple'
    except Exception as exc:errors.append('attachment_simple: '+repr(exc))
    try:
        t=agent.zot.item_template('attachment'); t.update({'title':'Full Text PDF','parentItem':parent_key,'linkMode':'linked_file','contentType':'application/pdf','path':str(path),'filename':path.name}); result=agent.zot.create_items([t])
        if isinstance(result,dict) and result.get('failed'):raise RuntimeError(str(result.get('failed')))
        return True,'created linked-file attachment'
    except Exception as exc:errors.append('linked_file fallback: '+repr(exc))
    detail='; '.join(errors) or 'unknown attachment failure'
    if log_file:
        try:Path(log_file).open('a',encoding='utf-8').write('\nPDF ATTACHMENT ERROR\n'+f'parent={parent_key} path={path}\n{detail}\n')
        except Exception:pass
    return False,detail

def token_estimate(path):
    if not path:return 0
    try:
        import pymupdf; doc=pymupdf.open(str(path)); chars=sum(len(page.get_text('text') or '') for page in doc); doc.close(); return max(1,int(chars/3.6))
    except Exception:
        try:return max(1,int(Path(path).stat().st_size/18))
        except Exception:return 0


def _json_url(url,timeout=20,headers=None):
    h={'User-Agent':'Literature-Agent/1.6.2 (+legal-open-access-resolver)'}; h.update(headers or {})
    req=urllib.request.Request(url,headers=h)
    with urllib.request.urlopen(req,timeout=timeout) as r:return json.loads(r.read().decode('utf-8','replace'))

def oa_candidates(meta, contact_email=''):
    """Return ordered legal/open full-text candidates. No publisher HTML scraping or auth bypass."""
    doi=str(meta.get('doi') or '').strip(); title=str(meta.get('title') or '').strip(); out=[]; seen=set()
    def add(source,url,version='',license_=''):
        u=str(url or '').strip()
        if u.startswith('http') and u not in seen: seen.add(u); out.append({'source':source,'url':u,'version':version,'license':license_})
    # deterministic repository IDs
    blob=' '.join(str(meta.get(k) or '') for k in ('url','extra','doi'))
    m=re.search(r'(?:arxiv(?:\.org/(?:abs|pdf)/|:))\s*([0-9]{4}\.[0-9]{4,5}(?:v\d+)?|[a-z-]+/[0-9]{7}(?:v\d+)?)',blob,re.I)
    if m:add('arXiv','https://export.arxiv.org/pdf/'+m.group(1)+'.pdf','preprint')
    # Europe PMC / PMC
    if doi or title:
        try:
            q=('DOI:'+doi) if doi else ('TITLE:"'+title.replace('"','')+'"')
            u='https://www.ebi.ac.uk/europepmc/webservices/rest/search?'+urllib.parse.urlencode({'query':q,'format':'json','pageSize':5})
            d=_json_url(u)
            for r0 in d.get('resultList',{}).get('result',[]):
                pmcid=str(r0.get('pmcid') or '')
                if pmcid:add('Europe PMC','https://www.ebi.ac.uk/europepmc/webservices/rest/'+pmcid+'/fullTextPDF','published')
        except Exception: pass
    if doi:
        qdoi=urllib.parse.quote(doi,safe='')
        # Unpaywall: requires contact email
        if contact_email:
            try:
                d=_json_url('https://api.unpaywall.org/v2/'+qdoi+'?'+urllib.parse.urlencode({'email':contact_email}))
                locs=[]
                if d.get('best_oa_location'):locs.append(d['best_oa_location'])
                locs += list(d.get('oa_locations') or [])
                for loc in locs:add('Unpaywall',loc.get('url_for_pdf'),loc.get('version',''),loc.get('license',''))
            except Exception: pass
        # OpenAlex all locations
        try:
            d=_json_url('https://api.openalex.org/works/https://doi.org/'+qdoi)
            locs=[]
            if d.get('best_oa_location'):locs.append(d['best_oa_location'])
            locs += list(d.get('locations') or [])
            for loc in locs:add('OpenAlex',loc.get('pdf_url'),loc.get('version',''),loc.get('license',''))
        except Exception: pass
        # Semantic Scholar OA
        try:
            d=_json_url('https://api.semanticscholar.org/graph/v1/paper/DOI:'+qdoi+'?fields=title,openAccessPdf')
            oa=d.get('openAccessPdf') or {}; add('Semantic Scholar',oa.get('url'),oa.get('status',''))
        except Exception: pass
        # Crossref full-text/TDM links; validator decides whether the URL is really a PDF
        try:
            d=_json_url('https://api.crossref.org/works/'+qdoi)
            for link in (d.get('message') or {}).get('link',[]) or []:
                ctype=str(link.get('content-type') or '').lower()
                if 'pdf' in ctype:add('Crossref',link.get('URL'),link.get('content-version',''))
        except Exception: pass
    return out

def download_candidates(meta,candidates,download_dir):
    d=Path(download_dir); d.mkdir(parents=True,exist_ok=True); path=d/safe_filename(meta.get('title'))
    if path.exists() and path.stat().st_size>1024:return path,{'source':'local-existing','url':'','version':'','license':''}
    for c in candidates:
        try:
            req=urllib.request.Request(c['url'],headers={'User-Agent':'Literature-Agent/1.6.2 legal OA recovery'})
            with urllib.request.urlopen(req,timeout=45) as r: ctype=(r.headers.get('Content-Type') or '').lower(); data=r.read(60*1024*1024)
            if data[:4]!=b'%PDF' and 'pdf' not in ctype: continue
            path.write_bytes(data)
            try:
                import pymupdf; doc=pymupdf.open(str(path)); pages=len(doc); doc.close()
                if pages<1: raise ValueError('empty PDF')
            except Exception: path.unlink(missing_ok=True); continue
            return path,c
        except Exception: continue
    return None,{}
