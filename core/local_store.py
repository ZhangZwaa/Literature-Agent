import json, sqlite3, hashlib
from pathlib import Path
from datetime import datetime, timezone

BASE = Path(__file__).resolve().parent.parent
DB = BASE / 'data' / 'literature.db'
DB.parent.mkdir(parents=True, exist_ok=True)

def conn():
    c=sqlite3.connect(DB, timeout=30)
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    c.executescript('''
    CREATE TABLE IF NOT EXISTS papers(item_key TEXT PRIMARY KEY, doi TEXT, title TEXT, abstract TEXT, record_text TEXT, analyzed_at TEXT, analysis_version TEXT, embedding_model TEXT, embedding_json TEXT);
    CREATE TABLE IF NOT EXISTS usage(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, operation TEXT, model TEXT, item_key TEXT, input_tokens INTEGER, output_tokens INTEGER, estimated_cost REAL);
    CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
    ''')
    return c

def upsert_paper(item_key, title='', doi='', abstract='', record_text='', analysis_version='0.6'):
    with conn() as c:
        c.execute('''INSERT INTO papers(item_key,doi,title,abstract,record_text,analyzed_at,analysis_version) VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(item_key) DO UPDATE SET doi=excluded.doi,title=excluded.title,abstract=excluded.abstract,record_text=excluded.record_text,analyzed_at=excluded.analyzed_at,analysis_version=excluded.analysis_version''',
        (item_key,doi,title,abstract,record_text,datetime.now(timezone.utc).isoformat(),analysis_version))

def record_usage(operation, model, item_key='', input_tokens=0, output_tokens=0, estimated_cost=0.0):
    with conn() as c:
        c.execute('INSERT INTO usage(ts,operation,model,item_key,input_tokens,output_tokens,estimated_cost) VALUES(?,?,?,?,?,?,?)',
                  (datetime.now(timezone.utc).isoformat(),operation,model,item_key,int(input_tokens),int(output_tokens),float(estimated_cost)))

def usage_summary():
    with conn() as c:
        today=c.execute("SELECT COUNT(*) calls,COALESCE(SUM(input_tokens),0) i,COALESCE(SUM(output_tokens),0) o FROM usage WHERE date(ts)=date('now')").fetchone()
        month=c.execute("SELECT COUNT(*) calls,COALESCE(SUM(input_tokens),0) i,COALESCE(SUM(output_tokens),0) o FROM usage WHERE strftime('%Y-%m',ts)=strftime('%Y-%m','now')").fetchone()
        papers=c.execute('SELECT COUNT(*) n FROM papers').fetchone()['n']
    def d(r):
        return {'calls':r['calls'],'input_tokens':r['i'],'output_tokens':r['o'],'total_tokens':r['i']+r['o']}
    return {'today':d(today),'month':d(month),'indexed_papers':papers}

def usage_history():
    with conn() as c:
        months=c.execute("""
            SELECT substr(ts,1,7) month, COUNT(*) calls,
                   COALESCE(SUM(input_tokens),0) input_tokens,
                   COALESCE(SUM(output_tokens),0) output_tokens
            FROM usage GROUP BY substr(ts,1,7) ORDER BY month DESC
        """).fetchall()
        result=[]
        for m in months:
            rows=c.execute("""
                SELECT id,ts,operation,model,item_key,input_tokens,output_tokens
                FROM usage WHERE substr(ts,1,7)=? ORDER BY ts DESC,id DESC
            """,(m['month'],)).fetchall()
            result.append({'month':m['month'],'calls':m['calls'],'input_tokens':m['input_tokens'],
                           'output_tokens':m['output_tokens'],'total_tokens':m['input_tokens']+m['output_tokens'],
                           'records':[dict(r) for r in rows]})
    return result

def clear_usage_history():
    with conn() as c:
        n=c.execute('SELECT COUNT(*) n FROM usage').fetchone()['n']
        c.execute('DELETE FROM usage')
    return n

def estimate_tokens(text):
    # Provider-independent fallback. Actual billing may differ.
    return max(1, round(len(text or '')/4))
