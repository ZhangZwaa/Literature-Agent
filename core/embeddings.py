import json, math
from .local_store import conn
MODEL_NAME='sentence-transformers/all-MiniLM-L6-v2'
_model=None

def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model=SentenceTransformer(MODEL_NAME)
    return _model

def embed_text(text):
    return _get_model().encode([text], normalize_embeddings=True)[0].tolist()

def index_paper(item_key, text):
    vec=embed_text(text[:12000])
    with conn() as c:
        c.execute('UPDATE papers SET embedding_model=?, embedding_json=? WHERE item_key=?',(MODEL_NAME,json.dumps(vec),item_key))
    return len(vec)

def related_local(item_key, limit=20):
    with conn() as c:
        target=c.execute('SELECT embedding_json FROM papers WHERE item_key=?',(item_key,)).fetchone()
        if not target or not target['embedding_json']: return []
        tv=json.loads(target['embedding_json'])
        rows=c.execute('SELECT item_key,title,embedding_json FROM papers WHERE item_key<>? AND embedding_json IS NOT NULL',(item_key,)).fetchall()
    out=[]
    for r in rows:
        v=json.loads(r['embedding_json'])
        score=sum(a*b for a,b in zip(tv,v))
        out.append({'key':r['item_key'],'title':r['title'],'similarity':score})
    return sorted(out,key=lambda x:x['similarity'],reverse=True)[:limit]
