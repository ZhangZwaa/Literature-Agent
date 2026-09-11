import json,time,urllib.parse,xml.etree.ElementTree as ET
from .common import http_text,effective_since

def search(query,max_results=25,policy=None,since=None,category='research'):
    policy=policy or {}; pp=policy.get('pubmed') or {}; term=query
    if pp.get('use_title_abstract',True):term=f'({query})[Title/Abstract]'
    pts=list(pp.get('publication_types') or [])
    if category=='review' and not pts:pts=['Review','Systematic Review','Meta-Analysis']
    if pts:term+=' AND ('+' OR '.join(f'"{x}"[Publication Type]' for x in pts)+')'
    dt=effective_since(since,pp.get('date_window_days',0))
    if dt:term+=f' AND ("{dt.strftime("%Y/%m/%d")}"[Date - Publication] : "3000"[Date - Publication])'
    base='https://eutils.ncbi.nlm.nih.gov/entrez/eutils/'; q=urllib.parse.urlencode({'db':'pubmed','term':term,'retmode':'json','retmax':max_results,'sort':'pub date'})
    ids=json.loads(http_text(base+'esearch.fcgi?'+q)).get('esearchresult',{}).get('idlist',[])
    if not ids:return []
    time.sleep(.36); root=ET.fromstring(http_text(base+'efetch.fcgi?'+urllib.parse.urlencode({'db':'pubmed','id':','.join(ids),'retmode':'xml'}))); out=[]
    for art in root.findall('.//PubmedArticle'):
        pmid=art.findtext('.//PMID','') or ''; node=art.find('.//ArticleTitle'); title=''.join(node.itertext()) if node is not None else ''; abstract=' '.join(''.join(x.itertext()) for x in art.findall('.//Abstract/AbstractText')); year=art.findtext('.//PubDate/Year') or art.findtext('.//ArticleDate/Year') or ''; doi=''
        for x in art.findall('.//ArticleId'):
            if x.attrib.get('IdType')=='doi':doi=x.text or ''
        authors=[]
        for a in art.findall('.//Author')[:8]:
            nm=' '.join(filter(None,[a.findtext('ForeName'),a.findtext('LastName')])).strip()
            if nm:authors.append(nm)
        out.append({'source':'pubmed','external_id':pmid,'title':title,'abstract':abstract,'year':year,'doi':doi,'authors':authors,'journal':art.findtext('.//Journal/Title') or '','url':f'https://pubmed.ncbi.nlm.nih.gov/{pmid}/'})
    return out
