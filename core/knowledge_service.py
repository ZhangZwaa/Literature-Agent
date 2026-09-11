import math
import re
from collections import Counter

STOP = {
    'the','and','for','with','from','that','this','these','those','into','onto','using','use','used','are','was','were','is','be','been','to','of','in','on','a','an','or','as','by','at','it','its','we','our','their','paper','papers','study','studies','method','methods','show','shows','what','which','how','why','can','could','would','should','do','does','did','about','find','library','my'
}

def _tokens(text):
    return [x for x in re.findall(r"[A-Za-z][A-Za-z0-9_.+-]{1,}|[\u4e00-\u9fff]{2,}", (text or '').lower()) if x not in STOP]

def _norm_title(text):
    return re.sub(r'[^a-z0-9\u4e00-\u9fff]+',' ',(text or '').lower()).strip()

def _doi(row):
    return str(row.get('doi') or '').strip().lower().replace('https://doi.org/','').replace('http://doi.org/','')

def _keywords(row):
    # Knowledge retrieval never consumes Research Cards or PDF text. Tags/extra are cheap metadata only.
    tags=row.get('tags') or []
    if isinstance(tags,list):
        tags=' '.join((x.get('tag','') if isinstance(x,dict) else str(x)) for x in tags)
    return (str(tags or '')+' '+str(row.get('extra') or ''))[:2500]

def _dedupe(items):
    out=[]; seen_keys=set(); seen_doi=set(); seen_titles=set()
    for row in items:
        key=str(row.get('key') or '')
        doi=_doi(row); nt=_norm_title(row.get('title'))
        if key and key in seen_keys: continue
        if doi and doi in seen_doi: continue
        if nt and nt in seen_titles: continue
        if key: seen_keys.add(key)
        if doi: seen_doi.add(doi)
        if nt: seen_titles.add(nt)
        out.append(row)
    return out

def _descendant_keys(tree, root_key):
    found=set()
    def walk(nodes, active=False):
        for n in nodes or []:
            here=active or n.get('key')==root_key
            if here: found.add(n.get('key'))
            walk(n.get('children') or [], here)
    walk(tree or [])
    return found

def _scope_items(cache, collection_key=None, include_subcollections=True):
    items=list((cache.get('items') or {}).values())
    if not collection_key: return items
    keys=_descendant_keys(cache.get('tree') or [], collection_key) if include_subcollections else {collection_key}
    return [r for r in items if keys.intersection(set(r.get('collection_keys') or []))]

def retrieve(cache, question, limit=8, collection_key=None, include_subcollections=True, prefilter_limit=40, threshold=28):
    """Two-stage local retrieval, zero LLM/network calls.

    Stage 1: title + keyword/tag/metadata prefilter.
    Stage 2: abstract rerank only for the prefiltered papers.
    Research Cards and PDF text are deliberately excluded.
    """
    items=_dedupe(_scope_items(cache, collection_key, include_subcollections))
    q=_tokens(question)
    if not q or not items:
        return [], {'screened':len(items),'prefiltered':0,'eligible':0}

    # Stage 1: cheap title/keyword coverage. Do not touch abstracts for the full library.
    stage1=[]
    qset=set(q)
    for row in items:
        title=str(row.get('title') or '')
        kw=_keywords(row)
        tt=_tokens(title); kt=_tokens(kw)
        tset=set(tt); kset=set(kt)
        title_hits=len(qset & tset); kw_hits=len(qset & kset)
        phrase_bonus=1.5 if any(' ' in x and x in title.lower() for x in [question.lower().strip()]) else 0
        # Exact query-term coverage dominates; title is much stronger than metadata keywords.
        score=(title_hits*4.0)+(kw_hits*1.25)+phrase_bonus
        # Keep a small lexical fallback for longer scientific terms.
        if score<=0:
            score=sum(0.35 for t in qset if any(t in x or x in t for x in tset if len(x)>4))
        if score>0: stage1.append((score,row))
    stage1.sort(key=lambda x:x[0],reverse=True)
    stage1=stage1[:max(int(prefilter_limit),int(limit)*3)]
    if not stage1:
        return [], {'screened':len(items),'prefiltered':0,'eligible':0}

    # Stage 2: BM25-style abstract rerank on only the small stage-1 pool.
    docs=[]; df=Counter()
    for s1,row in stage1:
        abstract=str(row.get('abstract') or '')
        at=_tokens(abstract); tf=Counter(at)
        docs.append((s1,row,abstract,tf,len(at)))
        for t in set(at): df[t]+=1
    n=len(docs); avgdl=sum(x[4] for x in docs)/max(1,n)
    raw=[]
    for s1,row,abstract,tf,dl in docs:
        bm=0.0
        for term in q:
            f=tf.get(term,0)
            if not f: continue
            idf=math.log(1+(n-df.get(term,0)+0.5)/(df.get(term,0)+0.5))
            bm += idf*(f*2.2/(f+1.2*(1-0.72+0.72*dl/max(1,avgdl))))
        coverage=len(set(q)&set(_tokens((row.get('title') or '')+' '+abstract)))/max(1,len(set(q)))
        raw_score=s1*1.8+bm*2.0+coverage*8.0
        raw.append((raw_score,coverage,row,abstract))
    raw.sort(key=lambda x:x[0],reverse=True)
    top=raw[0][0] if raw else 1.0
    out=[]
    for raw_score,coverage,row,abstract in raw:
        relative=100.0*raw_score/max(top,1e-9)
        # Require either meaningful query coverage or strong relative retrieval score.
        relevance=min(100, round(relative*0.72 + coverage*28,1))
        if relevance < float(threshold): continue
        evidence='abstract' if abstract.strip() else 'metadata-only'
        out.append({
            'key':row.get('key',''),'title':row.get('title') or 'Untitled','year':row.get('year',''),
            'doi':row.get('doi',''),'relevance':relevance,'abstract':abstract[:3200],
            'evidence':evidence,'has_pdf':bool(row.get('has_pdf')),
        })
        if len(out)>=max(1,int(limit)): break
    return out, {'screened':len(items),'prefiltered':len(stage1),'eligible':len(out)}

def build_prompt(question, rows, language='english', history=None):
    lang={'english':'English','chinese':'Chinese','bilingual':'English followed by concise Chinese'}.get(language,'English')
    sources=[]
    for i,r in enumerate(rows,1):
        body=r.get('abstract','').strip()
        if body:
            evidence=f"Abstract:\n{body}"
        else:
            evidence="No abstract is available. Use title/year metadata only and do not infer study details."
        sources.append(f"SOURCE [{i}]\nTitle: {r['title']}\nYear: {r.get('year','')}\nEvidence level: {r.get('evidence','abstract')}\n{evidence}")
    hist=''
    if history:
        lines=[]
        for i,t in enumerate(history,1):
            lines.append(f"Prior turn {i}: Q={t.get('question','')} | A excerpt={t.get('answer','')} | cited={'; '.join(t.get('cited_titles') or [])}")
        hist='\nCOMPACT CONVERSATION CONTEXT (no prior abstracts; use only to resolve follow-up references):\n'+'\n'.join(lines)+'\n'
    return f"""You are the Knowledge Agent for ONE researcher's local literature library.
Answer using ONLY the supplied title/abstract/metadata evidence. Never claim to have read a PDF or full Research Card in this task.
Do not use outside knowledge as evidence and do not invent facts. If the abstracts are insufficient, state what cannot be established.
Synthesize across papers rather than summarizing each separately unless asked.
Use inline citations like [1], [2]. Every substantive paper-specific claim must have a citation.
Treat abstract evidence as preliminary: do not overstate methods, numerical results, limitations, or causal conclusions beyond what the abstract explicitly supports.
Output language: {lang}.
Use concise Markdown headings/bullets when they improve readability.
Normally discuss each source once under its single most relevant category. If it is relevant elsewhere, cross-reference it briefly instead of repeating a full description.
Specific numbers, performance metrics, sample sizes, effect sizes, and mechanistic claims may be stated ONLY when they appear explicitly in the supplied abstract/metadata. Otherwise omit them or state that the abstract does not establish them.

{hist}\nQUESTION\n{question}\n\n""" + '\n\n'.join(sources)
