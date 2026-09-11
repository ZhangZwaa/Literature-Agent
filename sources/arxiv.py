import urllib.parse,xml.etree.ElementTree as ET
from .common import http_text

def search(query,max_results=25,policy=None,since=None,category='methods'):
    policy=policy or {}; ap=policy.get('arxiv') or {}; cats=[str(x).strip() for x in ap.get('categories',[]) if str(x).strip()]; cq='all:'+query
    if cats:cq='('+cq+') AND ('+' OR '.join('cat:'+x+'*' if x.endswith('q-bio') else 'cat:'+x for x in cats)+')'
    root=ET.fromstring(http_text('https://export.arxiv.org/api/query?'+urllib.parse.urlencode({'search_query':cq,'start':0,'max_results':max_results,'sortBy':'submittedDate','sortOrder':'descending'}))); ns={'a':'http://www.w3.org/2005/Atom'}; out=[]
    for e in root.findall('a:entry',ns):
        url=e.findtext('a:id','',ns); eid=url.rsplit('/',1)[-1]; pub=e.findtext('a:published','',ns); out.append({'source':'arxiv','external_id':eid,'title':' '.join((e.findtext('a:title','',ns) or '').split()),'abstract':' '.join((e.findtext('a:summary','',ns) or '').split()),'year':pub[:4],'doi':'','authors':[a.findtext('a:name','',ns) for a in e.findall('a:author',ns)][:8],'journal':'arXiv','url':url})
    return out
