import importlib.util
import json
import sys
import threading
import traceback
import webbrowser
import os
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, jsonify, request, Response, send_from_directory

SERVER_VERSION = "0.6.6"

BASE_DIR = Path(__file__).resolve().parent
AGENT_SCRIPT = BASE_DIR / "literature_agent.py"
HOST = "127.0.0.1"
PORT = 8765
SERVICE_VERSION = "1.6.2"
SETTINGS_FILE = BASE_DIR / "agent_settings.json"
DEFAULT_CONFIG_FILE = BASE_DIR / "config" / "default.json"
USER_PROFILE_FILE = BASE_DIR / "user" / "profile.json"
LIBRARY_CACHE_FILE = BASE_DIR / "data" / "dashboard_cache.json"
WATCH_CANDIDATES_FILE = BASE_DIR / "data" / "watch_candidates.json"
WATCH_STATE_FILE = BASE_DIR / "data" / "watch_state.json"
PAPER_REGISTRY_FILE = BASE_DIR / "data" / "paper_registry.json"
COMPARISONS_FILE = BASE_DIR / "data" / "comparisons.json"
RELATED_RESULTS_FILE = BASE_DIR / "data" / "related_results.json"
KNOWLEDGE_SESSIONS_FILE = BASE_DIR / "data" / "knowledge_sessions.json"
DOWNLOAD_DIR = BASE_DIR / "downloads"

def _download_dir():
    raw=str((read_settings().get('automation') or {}).get('pdf_download_directory','downloads')).strip() if 'read_settings' in globals() else 'downloads'
    q=Path(raw).expanduser()
    return q if q.is_absolute() else (BASE_DIR/q)
PROMPTS_FILE = BASE_DIR / "user" / "prompts.json"
LOCAL_LLM_FILE = BASE_DIR / "user" / "local_llm.json"
PRIMARY_LLM_FILE = BASE_DIR / "user" / "llm.json"
_cache_lock = threading.Lock()
_cache_syncing = False
_reconcile_lock = threading.Lock()
_reconcile_timer = None
RECONCILE_IDLE_SECONDS = 12
from core.local_store import usage_summary, usage_history, clear_usage_history, upsert_paper
from core.embeddings import index_paper, related_local
from core.knowledge_service import retrieve as knowledge_retrieve, build_prompt as knowledge_build_prompt
from core.metadata_enrichment import enrich_metadata
from core.research_card import research_card_for_item, strip_html, clear_card_cache
from core.settings_service import configure as configure_settings, deep_merge as _deep_merge, read_settings, write_settings, read_profile, write_profile, read_prompts, write_prompts
from sources.common import http_text as _http_text, parse_iso as _parse_iso
from sources.pubmed import search as _pubmed_search
from sources.europe_pmc import search as _europe_pmc_search
from sources.arxiv import search as _arxiv_search
from core.pdf_resolver import safe_filename as _safe_filename, download_urls as pdf_download_urls, attach_local_pdf as pdf_attach_local, token_estimate as pdf_token_estimate, oa_candidates as pdf_oa_candidates, download_candidates as pdf_download_candidates
from core.local_llm import LocalLLM, validate_watch_proposal
from core.llm_provider import PrimaryLLM
from core.prompt_service import read_all as role_prompts_read, write_all as role_prompts_write, restore as role_prompts_restore, ensure as role_prompts_ensure, combined as role_prompts_combined

APP_VERSION = "0.6.6"

WEB_DIR = BASE_DIR / 'web'
TEMPLATE_DIR = WEB_DIR / 'templates'
STATIC_DIR = WEB_DIR / 'static'
app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path='/static')
configure_settings(BASE_DIR, SETTINGS_FILE, DEFAULT_CONFIG_FILE, USER_PROFILE_FILE, PROMPTS_FILE)
_lock = threading.Lock()
_state = {
    "running": False, "mode": None, "item_key": None,
    "started_at": None, "finished_at": None,
    "last_result": None, "last_error": None,
    "stage": None, "stage_message": None,
}


def load_agent():
    if not AGENT_SCRIPT.exists():
        raise FileNotFoundError(f"Missing {AGENT_SCRIPT}")
    spec = importlib.util.spec_from_file_location("literature_agent_runtime", AGENT_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def set_state(**kwargs):
    with _lock:
        _state.update(kwargs)

def _is_zotero_version_conflict(exc):
    text=(type(exc).__name__+" "+str(exc)).lower()
    return ("preconditionfailed" in text or "precondition failed" in text or
            "code: 412" in text or "status 412" in text or "http 412" in text)

def _update_zotero_item_fresh(agent, item_key, mutate, retries=1):
    """Fresh GET -> minimal mutation -> PATCH, with one retry on Zotero HTTP 412."""
    last=None
    for attempt in range(retries+1):
        fresh=agent.zot.item(item_key)
        if not fresh or not fresh.get("data"):
            raise RuntimeError(f"Zotero item not found: {item_key}")
        changed=bool(mutate(fresh["data"]))
        if not changed:
            return fresh
        try:
            agent.zot.update_item(fresh)
            return agent.zot.item(item_key) or fresh
        except Exception as exc:
            last=exc
            if not _is_zotero_version_conflict(exc) or attempt>=retries:
                raise
    raise last

def run_job(mode, item_key=None, force=False):
    set_state(running=True, mode=mode, item_key=item_key,
              started_at=datetime.now().isoformat(timespec="seconds"),
              finished_at=None, last_error=None, last_result=None,
              stage="starting", stage_message="Starting analysis…")
    try:
        agent = load_agent()
        if mode == "scan":
            agent.main()
            result = "Scan complete"
        else:
            item = agent.zot.item(item_key)
            if not item or not item.get("data"):
                raise RuntimeError(f"Zotero item not found: {item_key}")
            def is_agent_analysis_note(child):
                d = child.get("data", {})
                if d.get("itemType") != "note":
                    return False
                note = str(d.get("note", "") or "")
                upper = note.upper()
                return agent.AI_NOTE_MARKER.lower() in note.lower() or "RESEARCH CARD" in upper

            if force:
                # Re-analyze deliberately replaces every agent-owned analysis note, including
                # newer Research Cards that do not contain the legacy AI_NOTE_MARKER string.
                for child in agent.get_children(item_key):
                    if is_agent_analysis_note(child):
                        agent.zot.delete_item(child)
                def remove_agent_tag(data):
                    tags=data.get("tags", [])
                    cleaned=[t for t in tags if t.get("tag", "").lower() != agent.AI_TAG.lower()]
                    if len(cleaned)==len(tags): return False
                    data["tags"]=cleaned; return True
                # Deleting a child note can advance the parent's Zotero version, so never PATCH
                # the parent object fetched before those deletes. Fresh-read it first.
                item=_update_zotero_item_fresh(agent,item_key,remove_agent_tag)
                clear_card_cache(item_key)
            elif mode == "manual_process":
                # The Paper View says Analyze only when no parseable Research Card exists.
                # Older agent summary notes can still make is_already_summarized() return True,
                # producing the observed instant no-op. Explicit Analyze upgrades that legacy
                # state: remove only agent-owned legacy/unparseable notes + processing tag,
                # then build a fresh Research Card from the attached PDF.
                clear_card_cache(item_key)
                existing_card = research_card_for_item(agent, item_key)
                if not existing_card:
                    for child in agent.get_children(item_key):
                        if is_agent_analysis_note(child):
                            agent.zot.delete_item(child)
                    def remove_agent_tag(data):
                        tags=data.get("tags", [])
                        cleaned=[t for t in tags if t.get("tag", "").lower() != agent.AI_TAG.lower()]
                        if len(cleaned)==len(tags): return False
                        data["tags"]=cleaned; return True
                    item=_update_zotero_item_fresh(agent,item_key,remove_agent_tag)
            elif mode == "process":
                # Routine processing keeps the cheap tag/note short-circuit.
                pass
            def progress(stage, message):
                set_state(stage=stage, stage_message=message)
            status = agent.process_paper(item, progress=progress)
            if mode == "manual_process" and status == "already_done":
                clear_card_cache(item_key)
                if not research_card_for_item(agent, item_key):
                    raise RuntimeError("Manual analysis was skipped by legacy summary state; the item still has no parseable Research Card")
            clear_card_cache(item_key)
            _patch_cached_paper_state(item_key,has_pdf=True,agent=agent)
            _schedule_cache_reconcile()
            result = f"{mode}: {status}"
        set_state(last_result=result, stage="done", stage_message=result)
    except Exception as exc:
        set_state(last_error=f"{type(exc).__name__}: {exc}", stage="error", stage_message=f"{type(exc).__name__}: {exc}")
        try:
            with open(BASE_DIR / "agent.log", "a", encoding="utf-8") as f:
                f.write("\nSERVER JOB ERROR\n" + traceback.format_exc() + "\n")
        except Exception:
            pass
    finally:
        set_state(running=False, finished_at=datetime.now().isoformat(timespec="seconds"))

def start_job(mode, item_key=None, force=False):
    with _lock:
        if _state["running"]:
            return False
    threading.Thread(target=run_job, args=(mode, item_key, force), daemon=True).start()
    return True

def library_snapshot():
    agent = load_agent()
    keys = agent.get_target_collection_keys()
    items = agent.collect_unique_items(keys)
    summarized = pending = no_pdf = 0
    recent = []
    for item in items:
        d = item.get("data", {})
        key = d.get("key")
        done = agent.has_ai_summary_note(key)
        pdf = agent.find_pdf_attachment(key)
        if done:
            summarized += 1
        elif pdf:
            pending += 1
        else:
            no_pdf += 1
        card = research_card_for_item(agent, key)
        recent.append({
            "key": key,
            "title": d.get("title", "Untitled"),
            "year": d.get("date", "")[:4],
            "summarized": done,
            "has_pdf": bool(pdf),
            "card": card,
        })
    return {
        "total": len(items), "summarized": summarized,
        "pending": pending, "no_pdf": no_pdf,
        "items": recent[-30:][::-1],
    }



ZOTERO_ROLE_ORDER = ("inbox", "research", "methods", "review", "projects")
ZOTERO_ROLE_PREFIX = {"inbox":"00", "research":"01", "methods":"02", "review":"03", "projects":"04"}

def _zotero_role_sets(agent, collections=None):
    """Resolve semantic role scopes. Inbox is one key; other roles may contain many top-level keys."""
    collections=collections if collections is not None else agent.get_all_collections()
    tops=[c for c in collections if not c.get('data',{}).get('parentCollection')]; by={c['data']['key']:c for c in tops}
    cfg=read_settings().get('zotero_integration') or {}; saved_sets=cfg.get('role_sets') or {}; legacy=cfg.get('roles') or {}
    out={}
    for role in ZOTERO_ROLE_ORDER:
        vals=saved_sets.get(role)
        if role=='inbox': vals=[str(cfg.get('inbox_key') or legacy.get('inbox') or '').strip()]
        elif not isinstance(vals,list): vals=[str(legacy.get(role) or '').strip()]
        vals=[str(x).strip() for x in (vals or []) if str(x).strip() in by]
        if not vals:
            matches=[c['data']['key'] for c in tops if str(c['data'].get('name') or '').startswith(ZOTERO_ROLE_PREFIX[role])]
            if len(matches)==1: vals=matches
        out[role]=list(dict.fromkeys(vals))
    return out

def _zotero_role_map(agent, collections=None):
    """Default destination per role; backward-compatible callers still receive one key."""
    collections=collections if collections is not None else agent.get_all_collections(); sets=_zotero_role_sets(agent,collections)
    cfg=read_settings().get('zotero_integration') or {}; defaults=cfg.get('role_defaults') or {}; legacy=cfg.get('roles') or {}; out={}
    for role in ZOTERO_ROLE_ORDER:
        if role=='inbox':
            if sets[role]: out[role]=sets[role][0]
        else:
            k=str(defaults.get(role) or legacy.get(role) or '').strip(); out[role]=k if k in sets[role] else (sets[role][0] if sets[role] else '')
            if not out[role]: out.pop(role,None)
    return out

def _zotero_integration_snapshot(agent):
    collections=agent.get_all_collections(); sets=_zotero_role_sets(agent,collections); defaults=_zotero_role_map(agent,collections)
    tops=[c for c in collections if not c.get('data',{}).get('parentCollection')]
    rows=sorted([{'key':c['data']['key'],'name':c['data'].get('name','')} for c in tops],key=lambda x:x['name'].lower())
    names={x['key']:x['name'] for x in rows}; cfg=read_settings().get('zotero_integration') or {}
    return {'roles':defaults,'role_sets':sets,'role_defaults':defaults,'role_names':{r:names.get(k,'') for r,k in defaults.items()},'collections':rows,'sync_status_tags':bool(cfg.get('sync_status_tags',True))}

def _managed_root_keys(agent, collections=None):
    sets=_zotero_role_sets(agent,collections); return {k for vals in sets.values() for k in vals}

def collection_catalog(agent):
    """Collections under the Zotero roots assigned semantic Literature Agent roles."""
    collections = agent.get_all_collections()
    by_key = {c["data"]["key"]: c for c in collections}
    managed_roots=_managed_root_keys(agent,collections)
    # Backward-compatible fallback for a brand-new install before role discovery succeeds.
    if not managed_roots:
        managed_roots={c['data']['key'] for c in collections if not c['data'].get('parentCollection') and str(c['data'].get('name') or '')[:1].isdigit()}
    rows = []
    for c in collections:
        d = c["data"]
        parts = [d.get("name", "")]
        parent = d.get("parentCollection", False)
        seen = set(); root_key=d.get('key')
        while parent and parent not in seen and parent in by_key:
            seen.add(parent); root_key=parent; pd = by_key[parent]["data"]
            parts.insert(0, pd.get("name", "")); parent = pd.get("parentCollection", False)
        if root_key in managed_roots:
            rows.append({"key":d["key"],"name":d.get("name", ""),"path":" -> ".join(parts),"parts":parts,"root":parts[0] if parts else '',"root_key":root_key,"parent":d.get("parentCollection",False) or None})
    rows.sort(key=lambda x: x["parts"])
    return rows


def collection_tree_snapshot(agent):
    catalog = collection_catalog(agent); allowed = {r["key"]:r for r in catalog}
    children = {k:[] for k in allowed}; roots=[]
    for key,row in allowed.items():
        parent=row.get("parent")
        (children[parent] if parent in allowed else roots).append(key)
    for ks in children.values(): ks.sort(key=lambda k:allowed[k]["name"].lower())
    roots.sort(key=lambda k:allowed[k]["name"].lower())
    direct={}
    for key in allowed:
        rows=[]
        try:
            for item in agent.zot.collection_items_top(key):
                d=item.get("data",{})
                if d.get("itemType") in ("attachment","note"): continue
                ik=d.get("key")
                if not ik: continue
                rows.append({"key":ik,"title":d.get("title","Untitled"),"year":(d.get("date","") or "")[:4],"summarized":bool(agent.has_ai_summary_note(ik)),"has_pdf":bool(agent.find_pdf_attachment(ik))})
        except Exception: pass
        direct[key]=rows
    def build(key):
        r=allowed[key]
        return {"key":key,"name":r["name"],"path":r["path"],"items":direct.get(key,[]),"children":[build(k) for k in children.get(key,[])]}
    return [build(k) for k in roots]


def _read_dashboard_cache():
    try:
        if LIBRARY_CACHE_FILE.exists():
            return json.loads(LIBRARY_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"ready": False, "syncing": _cache_syncing, "tree": [], "items": {}, "total": 0, "summarized": 0, "pending": 0, "no_pdf": 0}

def _write_dashboard_cache(payload):
    LIBRARY_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = LIBRARY_CACHE_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    tmp.replace(LIBRARY_CACHE_FILE)

def _raw_paper_identity(data):
    """Identity from raw Zotero item data; used during cache construction before normalized rows exist."""
    import re as _re
    doi=_norm_doi(data.get('DOI')) if '_norm_doi' in globals() else str(data.get('DOI') or '').strip().lower()
    if doi: return 'doi:'+doi
    blob=' '.join(str(data.get(k) or '') for k in ('url','extra'))
    m=_re.search(r'arxiv(?:\.org/(?:abs|pdf)/|:)\s*([0-9]{4}\.[0-9]{4,5}(?:v\d+)?|[a-z-]+/[0-9]{7}(?:v\d+)?)',blob,_re.I)
    if m: return 'arxiv:'+_re.sub(r'v\d+$','',m.group(1).lower())
    title=_norm_title(data.get('title')) if '_norm_title' in globals() else _re.sub(r'[^a-z0-9]+',' ',str(data.get('title') or '').lower()).strip()
    year=(str(data.get('date') or '')[:4])
    return 'fp:'+hashlib.sha256((title+'|'+year).encode('utf-8')).hexdigest()[:24]

def _resolve_cached_key(cache, item_key, prefer_pdf=False):
    """Resolve any Zotero duplicate key to the logical paper's canonical/action key."""
    aliases=cache.get('aliases') or {}
    canonical=aliases.get(item_key,item_key)
    row=(cache.get('items') or {}).get(canonical)
    if not row: return item_key
    if prefer_pdf:
        for k in row.get('zotero_keys') or [canonical]:
            rr=(cache.get('record_states') or {}).get(k) or {}
            if rr.get('has_pdf'): return k
    return row.get('action_key') or canonical

def _build_dashboard_cache():
    global _cache_syncing
    with _cache_lock:
        if _cache_syncing: return
        _cache_syncing=True
    try:
        agent=load_agent(); catalog=collection_catalog(agent); valid={r['key']:r for r in catalog}
        children={k:[] for k in valid}; roots=[]
        for key,row in valid.items():
            parent=row.get('parent'); (children[parent] if parent in valid else roots).append(key)
        for ks in children.values(): ks.sort(key=lambda k:valid[k]['name'].lower())
        roots.sort(key=lambda k:valid[k]['name'].lower())
        raw={}
        for ck in valid:
            try:
                for it in agent.zot.collection_items_top(ck):
                    d=it.get('data',{})
                    if d.get('itemType') in ('attachment','note'): continue
                    if d.get('key'): raw[d['key']]=it
            except Exception: continue
        # Inspect every Zotero record, then collapse duplicates by scholarly identity.
        groups={}; states={}
        for key,it in raw.items():
            d=it.get('data',{}); pid=_raw_paper_identity(d)
            try: done=bool(agent.has_ai_summary_note(key))
            except Exception: done=False
            try: pdf=bool(agent.find_pdf_attachment(key))
            except Exception: pdf=False
            try: card=research_card_for_item(agent,key)
            except Exception: card=None
            states[key]={'has_pdf':pdf,'summarized':done,'has_card':bool(card)}
            groups.setdefault(pid,[]).append((key,it,card,pdf,done))
        item_map={}; aliases={}; direct={k:[] for k in valid}; summarized=pending=no_pdf=0
        for pid,members in groups.items():
            # Prefer the record with a Card, then PDF, then oldest key deterministically.
            members.sort(key=lambda z:(bool(z[2]),z[3],z[4],z[0]),reverse=True)
            canonical=members[0][0]
            action=next((z[0] for z in members if z[3]),canonical)
            base=members[0][1].get('data',{}); card=next((z[2] for z in members if z[2]),None)
            has_pdf=any(z[3] for z in members); done=any(z[4] for z in members)
            cols=[]
            for _,it,_,_,_ in members:
                for ck in it.get('data',{}).get('collections',[]) or []:
                    if ck in valid and ck not in cols: cols.append(ck)
            keys=[z[0] for z in members]
            for k in keys: aliases[k]=canonical
            if done: summarized+=1
            elif has_pdf: pending+=1
            else: no_pdf+=1
            row={'key':canonical,'action_key':action,'zotero_keys':keys,'duplicate_count':len(keys),'paper_id':pid,
                 'title':base.get('title','Untitled'),'year':(base.get('date','') or '')[:4],'published':base.get('date','') or '',
                 'date_added':base.get('dateAdded','') or '','doi':base.get('DOI','') or '','url':base.get('url','') or '',
                 'abstract':base.get('abstractNote','') or '','extra':base.get('extra','') or '','tags':base.get('tags',[]) or [],
                 'summarized':done,'has_pdf':has_pdf,'card':card,'collection_keys':cols}
            item_map[canonical]=row
            mini={k:row[k] for k in ('key','title','year','summarized','has_pdf')}; mini['duplicate_count']=len(keys)
            for ck in cols: direct[ck].append(dict(mini))
        for rows in direct.values(): rows.sort(key=lambda r:r['title'].lower())
        def build(k):
            r=valid[k]; return {'key':k,'name':r['name'],'path':r['path'],'items':direct[k],'children':[build(c) for c in children[k]]}
        payload={'ready':True,'syncing':False,'updated_at':datetime.now().isoformat(timespec='seconds'),'tree':[build(k) for k in roots],
                 'items':item_map,'aliases':aliases,'record_states':states,'total':len(item_map),'summarized':summarized,'pending':pending,'no_pdf':no_pdf}
        _write_dashboard_cache(payload)
    except Exception:
        try:
            with open(BASE_DIR/'agent.log','a',encoding='utf-8') as f:f.write('\nCACHE SYNC ERROR\n'+traceback.format_exc()+'\n')
        except Exception: pass
    finally: _cache_syncing=False

def _start_cache_sync():
    global _cache_syncing
    with _cache_lock:
        if _cache_syncing: return False
    threading.Thread(target=_build_dashboard_cache, daemon=True).start()
    return True

def _cancel_scheduled_reconcile():
    global _reconcile_timer
    with _reconcile_lock:
        if _reconcile_timer is not None:
            try: _reconcile_timer.cancel()
            except Exception: pass
            _reconcile_timer=None

def _schedule_cache_reconcile(delay=RECONCILE_IDLE_SECONDS):
    """Debounced full Zotero reconciliation after a burst of local writes."""
    global _reconcile_timer
    def run():
        global _reconcile_timer
        with _reconcile_lock: _reconcile_timer=None
        _start_cache_sync()
    with _reconcile_lock:
        if _reconcile_timer is not None:
            try: _reconcile_timer.cancel()
            except Exception: pass
        _reconcile_timer=threading.Timer(float(delay),run)
        _reconcile_timer.daemon=True
        _reconcile_timer.start()

def _rebuild_tree_items(cache):
    """Rebuild only collection membership from cached logical papers; no Zotero reads."""
    direct={}
    def collect(nodes):
        for n in nodes or []:
            direct[n.get('key')]=[]
            collect(n.get('children') or [])
    collect(cache.get('tree') or [])
    for row in (cache.get('items') or {}).values():
        mini={k:row.get(k) for k in ('key','title','year','summarized','has_pdf')}
        mini['duplicate_count']=int(row.get('duplicate_count') or 1)
        for ck in row.get('collection_keys') or []:
            if ck in direct: direct[ck].append(dict(mini))
    for rows in direct.values(): rows.sort(key=lambda r:(r.get('title') or '').lower())
    def apply(nodes):
        for n in nodes or []:
            n['items']=direct.get(n.get('key'),[])
            apply(n.get('children') or [])
    apply(cache.get('tree') or [])

def _patch_cached_locations(item_key, collection_keys):
    """Immediately reflect a successful filing operation in dashboard_cache.json."""
    with _cache_lock:
        cache=_read_dashboard_cache()
        canonical=(cache.get('aliases') or {}).get(item_key,item_key)
        row=(cache.get('items') or {}).get(canonical)
        if not row: return False
        row['collection_keys']=list(dict.fromkeys(collection_keys or []))
        _rebuild_tree_items(cache)
        cache['updated_at']=datetime.now().isoformat(timespec='seconds')
        _write_dashboard_cache(cache)
        return True

def _recount_cache(cache):
    summarized=pending=no_pdf=0
    for row in (cache.get('items') or {}).values():
        if row.get('summarized') or row.get('card'): summarized+=1
        elif row.get('has_pdf'): pending+=1
        else: no_pdf+=1
    cache['total']=len(cache.get('items') or {})
    cache['summarized']=summarized; cache['pending']=pending; cache['no_pdf']=no_pdf

def _patch_cached_paper_state(item_key, *, has_pdf=None, card_marker=False, agent=None):
    """Patch PDF/Card state after an operation we just completed, without a full Zotero scan."""
    with _cache_lock:
        cache=_read_dashboard_cache(); canonical=(cache.get('aliases') or {}).get(item_key,item_key)
        row=(cache.get('items') or {}).get(canonical)
        if not row: return False
        if has_pdf is not None: row['has_pdf']=bool(has_pdf)
        if agent is not None:
            try:
                card=research_card_for_item(agent,item_key)
                if card:
                    row['card']=card; row['summarized']=True
            except Exception: pass
        elif card_marker:
            row['summarized']=True
        _recount_cache(cache); _rebuild_tree_items(cache)
        cache['updated_at']=datetime.now().isoformat(timespec='seconds'); _write_dashboard_cache(cache)
        return True

def _patch_cached_new_watch_item(parent_key, cand, inbox_key, has_pdf=False):
    if not parent_key: return False
    with _cache_lock:
        cache=_read_dashboard_cache()
        pseudo={'key':parent_key,'title':cand.get('title','Untitled'),'date':cand.get('year',''),'DOI':cand.get('doi',''),'url':cand.get('url',''),'extra':''}
        pid=_raw_paper_identity(pseudo)
        canonical=next((k for k,r in (cache.get('items') or {}).items() if r.get('paper_id')==pid),parent_key)
        if canonical in (cache.get('items') or {}):
            row=cache['items'][canonical]; keys=row.setdefault('zotero_keys',[canonical])
            if parent_key not in keys: keys.append(parent_key)
            row['duplicate_count']=len(keys); row['has_pdf']=bool(row.get('has_pdf') or has_pdf)
            if inbox_key and inbox_key not in row.setdefault('collection_keys',[]): row['collection_keys'].append(inbox_key)
        else:
            row={'key':parent_key,'action_key':parent_key,'zotero_keys':[parent_key],'duplicate_count':1,'paper_id':pid,'title':cand.get('title','Untitled'),
                 'year':str(cand.get('year') or '')[:4],'published':str(cand.get('year') or ''),'date_added':datetime.now().isoformat(timespec='seconds'),
                 'doi':cand.get('doi','') or '','url':cand.get('url','') or '','abstract':cand.get('abstract','') or '','extra':'','tags':[],
                 'summarized':False,'has_pdf':bool(has_pdf),'card':None,'collection_keys':[inbox_key] if inbox_key else []}
            cache.setdefault('items',{})[parent_key]=row; canonical=parent_key
        cache.setdefault('aliases',{})[parent_key]=canonical
        _recount_cache(cache); _rebuild_tree_items(cache); cache['updated_at']=datetime.now().isoformat(timespec='seconds'); _write_dashboard_cache(cache)
        return True

def _patch_cached_collection_name(collection_key, name):
    with _cache_lock:
        cache=_read_dashboard_cache(); changed=False
        def walk(nodes):
            nonlocal changed
            for n in nodes or []:
                if n.get('key')==collection_key:
                    n['name']=name; changed=True
                walk(n.get('children') or [])
        walk(cache.get('tree') or [])
        if changed:
            cache['updated_at']=datetime.now().isoformat(timespec='seconds')
            _write_dashboard_cache(cache)
        return changed

@app.get('/cache')
def cache_api():
    x=_read_dashboard_cache(); x['syncing']=_cache_syncing
    return jsonify(ok=True, **x)

@app.post('/cache/sync')
def cache_sync_api():
    _cancel_scheduled_reconcile()
    started=_start_cache_sync()
    return jsonify(ok=True, started=started, message='Background sync started' if started else 'Sync already running')

@app.get('/cache/item/<item_key>')
def cache_item_api(item_key):
    x=_read_dashboard_cache(); row=(x.get('items') or {}).get(item_key)
    if not row: return jsonify(ok=False,message='Item not cached yet'),404
    return jsonify(ok=True, **row)

@app.get('/zotero/integration')
def zotero_integration_get():
    try:
        agent=load_agent(); snap=_zotero_integration_snapshot(agent)
        return jsonify(ok=True,**snap)
    except Exception as exc:
        return jsonify(ok=False,error=f"{type(exc).__name__}: {exc}"),500

@app.post('/zotero/integration')
def zotero_integration_set():
    data=request.get_json(silent=True) or {}; role_sets=data.get('role_sets') or {}; defaults=data.get('role_defaults') or {}
    try:
        agent=load_agent(); tops={c.get('data',{}).get('key') for c in agent.get_all_collections() if not c.get('data',{}).get('parentCollection')}
        inbox=str((role_sets.get('inbox') or [data.get('inbox_key') or ''])[0] or '').strip()
        if inbox and inbox not in tops:return jsonify(ok=False,message='Inbox must reference a top-level Zotero collection'),400
        cleaned={'inbox':[inbox] if inbox else []}; clean_defaults={}
        for role in ('research','methods','review','projects'):
            vals=list(dict.fromkeys(str(x).strip() for x in (role_sets.get(role) or []) if str(x).strip()))
            if any(k not in tops for k in vals):return jsonify(ok=False,message=f'{role} contains an invalid top-level Zotero collection'),400
            cleaned[role]=vals; d=str(defaults.get(role) or '').strip()
            if d and d not in vals:return jsonify(ok=False,message=f'{role} default destination must be selected in that role'),400
            if vals:clean_defaults[role]=d or vals[0]
        payload={'role_sets':cleaned,'role_defaults':clean_defaults,'inbox_key':inbox,'sync_status_tags':bool(data.get('sync_status_tags',True))}
        write_settings({'zotero_integration':payload}); _start_cache_sync()
        return jsonify(ok=True,message='Zotero collection sets saved',**_zotero_integration_snapshot(agent))
    except Exception as exc:return jsonify(ok=False,error=f'{type(exc).__name__}: {exc}'),500

@app.get("/settings")
def get_settings():
    return jsonify(ok=True, **read_settings())

@app.post("/settings")
def set_settings():
    try:
        data = request.get_json(silent=True) or {}
        auto=data.get('automation') or {}
        if 'pdf_download_directory' in auto:
            raw=str(auto.get('pdf_download_directory') or 'downloads').strip(); q=Path(raw).expanduser(); q=q if q.is_absolute() else BASE_DIR/q
            try: q.mkdir(parents=True,exist_ok=True); test=q/'.literature_agent_write_test'; test.write_text('ok',encoding='utf-8'); test.unlink()
            except Exception as exc: return jsonify(ok=False,error=f'PDF download directory is not writable: {exc}'),400
        return jsonify(ok=True, **write_settings(data))
    except Exception as exc:
        return jsonify(ok=False, error=f"{type(exc).__name__}: {exc}"), 400

@app.get('/analysis-instructions')
def analysis_instructions_get(): return jsonify(ok=True,instructions=role_prompts_read(BASE_DIR),folder=str((BASE_DIR/'user'/'prompts').resolve()))

@app.post('/analysis-instructions')
def analysis_instructions_set():
    return jsonify(ok=True,instructions=role_prompts_write(BASE_DIR,(request.get_json(silent=True) or {}).get('instructions') or {}),message='Analysis instructions saved')

@app.post('/analysis-instructions/restore')
def analysis_instructions_restore(): return jsonify(ok=True,instructions=role_prompts_restore(BASE_DIR),message='Defaults restored')

@app.post('/system/open-prompt-folder')
def open_prompt_folder():
    try:
        folder,_=role_prompts_ensure(BASE_DIR); folder=folder.resolve()
        if sys.platform.startswith('win'): os.startfile(str(folder))
        elif sys.platform=='darwin': import subprocess; subprocess.Popen(['open',str(folder)])
        else: import subprocess; subprocess.Popen(['xdg-open',str(folder)])
        return jsonify(ok=True,message='Prompt folder opened',path=str(folder))
    except Exception as exc:return jsonify(ok=False,error=f'{type(exc).__name__}: {exc}'),500

@app.post('/system/select-folder')
def system_select_folder():
    if request.remote_addr not in ('127.0.0.1','::1'):return jsonify(ok=False,message='Native folder picker is available only on the local machine'),403
    try:
        import tkinter as tk
        from tkinter import filedialog
        data=request.get_json(silent=True) or {}; initial=str(data.get('initial') or BASE_DIR)
        q=Path(initial).expanduser(); q=q if q.is_absolute() else BASE_DIR/q
        root=tk.Tk(); root.withdraw(); root.attributes('-topmost',True)
        chosen=filedialog.askdirectory(initialdir=str(q if q.exists() else BASE_DIR),title='Choose PDF download directory'); root.destroy()
        return jsonify(ok=True,path=chosen or '')
    except Exception as exc:return jsonify(ok=False,error=f'{type(exc).__name__}: {exc}'),500

@app.get("/usage")
def usage():
    return jsonify(ok=True, **usage_summary())

@app.get("/usage/history")
def usage_history_api():
    return jsonify(ok=True, months=usage_history())

@app.post("/usage/clear")
def usage_clear_api():
    deleted = clear_usage_history()
    return jsonify(ok=True, deleted=deleted, message=f"Cleared {deleted} usage records")

@app.get("/watch/settings")
def watch_settings():
    st=read_settings()
    return jsonify(ok=True, watch=st.get("watch",{}), limits=st.get("limits",{}), inbox_retention_days=st.get("inbox_retention_days",14), profile=read_profile())

@app.post("/watch/settings")
def save_watch_settings():
    data=request.get_json(silent=True) or {}
    st=write_settings({k:v for k,v in data.items() if k in ("watch","limits","inbox_retention_days","source_policy")})
    if isinstance(data.get("profile"),dict): write_profile(data["profile"])
    return jsonify(ok=True, watch=st.get("watch",{}), limits=st.get("limits",{}), profile=read_profile())

@app.get('/prompts')
def prompts_get(): return jsonify(ok=True,prompts=read_prompts())

@app.post('/prompts')
def prompts_set():
    return jsonify(ok=True,prompts=write_prompts((request.get_json(silent=True) or {}).get('prompts') or {}))

@app.post('/watch/clear-search')
def clear_search_conditions():
    st=read_settings(); watch=st.get('watch') or {}
    for k in ('research','methods','review'):
        if isinstance(watch.get(k),dict): watch[k]['query']=''
    write_settings({'watch':watch})
    prof=read_profile(); prof['watch_instructions']=''; write_profile(prof)
    return jsonify(ok=True,message='Search conditions cleared')

@app.get("/llm/settings")
def primary_llm_settings_get():
    llm=PrimaryLLM(PRIMARY_LLM_FILE,BASE_DIR)
    return jsonify(ok=True,**llm.public_config(),status=llm.health(False))

@app.post("/llm/settings")
def primary_llm_settings_set():
    try:
        llm=PrimaryLLM(PRIMARY_LLM_FILE,BASE_DIR); cfg=llm.save_config(request.get_json(silent=True) or {})
        return jsonify(ok=True,**cfg,status=llm.health(False),message="Primary LLM settings saved. Restart the server before running analyses.")
    except Exception as exc:return jsonify(ok=False,error=f"{type(exc).__name__}: {exc}"),400

@app.post("/llm/test")
def primary_llm_test():
    llm=PrimaryLLM(PRIMARY_LLM_FILE,BASE_DIR); h=llm.health(True); h["service_version"]=SERVICE_VERSION
    return jsonify(h),200

@app.get("/local-llm/settings")
def local_llm_settings_get():
    llm=LocalLLM(LOCAL_LLM_FILE)
    payload={'ok':True, **llm.public_config(), 'status':llm.health()}; return jsonify(payload)

@app.post("/local-llm/settings")
def local_llm_settings_set():
    try:
        llm=LocalLLM(LOCAL_LLM_FILE)
        cfg=llm.save_config(request.get_json(silent=True) or {})
        payload={'ok':True, **llm.public_config(), 'status':llm.health()}; return jsonify(payload)
    except Exception as exc:
        return jsonify(ok=False,error=f"{type(exc).__name__}: {exc}"),400

@app.post("/local-llm/test")
def local_llm_test():
    """Test the saved local-LLM configuration even when it is disabled. Always return JSON."""
    try:
        llm=LocalLLM(LOCAL_LLM_FILE)
        health=llm.health(ignore_enabled=True, probe_generate=True)
        payload=dict(health); payload['service_version']=SERVICE_VERSION; return jsonify(payload), 200
    except Exception as exc:
        try:
            with open(BASE_DIR/"agent.log","a",encoding="utf-8") as f:
                f.write("\nLOCAL LLM TEST ERROR\n"+traceback.format_exc()+"\n")
        except Exception:
            pass
        return jsonify(ok=False,service_version=SERVICE_VERSION,model='',error=f"{type(exc).__name__}: {exc}"),200

@app.get("/local-llm/status")
def local_llm_status():
    llm=LocalLLM(LOCAL_LLM_FILE)
    health=llm.health()
    return jsonify(ok=True, route_ok=True, service_version=SERVICE_VERSION, config=llm.public_config(), health=health)

@app.post("/watch/compile")
def compile_watch_instructions():
    data=request.get_json(silent=True) or {}; text=str(data.get("instructions","")).strip()
    if not text: return jsonify(ok=False,message="instructions required"),400
    current=read_settings(); profile=read_profile()
    base={k:current.get(k) for k in ('watch','limits','inbox_retention_days')}
    prompt=f"""{read_prompts()['watch_compile']}

Convert the user's natural-language literature watch instructions into a SAFE proposed configuration change. Do not execute searches. Return JSON only with keys watch, limits, inbox_retention_days, profile. Each watch.research/methods/review object may include a query string containing the actual topical search terms to send to literature APIs. Convert topical interests into concise Boolean-style keyword queries suitable for the configured literature sources.

CRITICAL CONFIG EDITING RULES:
1. Modify ONLY fields explicitly requested by the user.
2. NEVER infer, optimize, simplify, remove, add, or replace settings that the user did not explicitly ask to change.
3. Preserve every unspecified field EXACTLY as it currently appears in CURRENT and PROFILE.
4. In particular, NEVER change sources, query, threshold, frequency, max_candidates, or enabled unless the user explicitly requests that specific field.
5. "Keep unchanged", "do not change", "不变", "保持原样", and equivalent wording means copy the CURRENT value exactly.
6. An empty current value must remain empty unless the user explicitly asks to set it.
7. Do not restore previously cleared search conditions, queries, or watch instructions.
8. Do not remove fields merely because their current value is empty.
9. Do not make recommendations or optimize the configuration. You are a literal configuration editor, not a configuration advisor.
10. Return the COMPLETE proposed configuration after applying ONLY the explicitly requested edits. Unmentioned fields must be copied unchanged.
11. If the user explicitly supplies a new research topic/search topic, you may rewrite that topic into a concise Boolean query. Otherwise preserve the existing query exactly, including an empty query.
12. If the user explicitly changes one field within a category, preserve all other fields in that category exactly.

EXAMPLE:
CURRENT methods.sources=["pubmed","europe_pmc","arxiv"], methods.threshold=78.
USER: "Methods threshold 改成75"
CORRECT: methods.sources remains ["pubmed","europe_pmc","arxiv"] and methods.threshold becomes 75.
WRONG: removing europe_pmc, changing the query, frequency, max_candidates, or any other unrequested field.

Use the following user-editable shared taxonomy when interpreting Research / Methods / Review:
{role_prompts_combined(BASE_DIR)}

Research/Methods/Review meanings come from that taxonomy. Allowed frequencies: daily, weekly, biweekly, monthly. thresholds 0-100. Allowed sources: pubmed,europe_pmc,arxiv,biorxiv,medrxiv. Never increase max_source_requests_per_day above 500 or max_llm_screenings_per_day above 100.
CURRENT={json.dumps(base,ensure_ascii=False)}
PROFILE={json.dumps(profile,ensure_ascii=False)}
USER INSTRUCTIONS={text}"""

    # Low-risk parser task: local LLM first. Scientific screening uses the configured Primary LLM.
    llm=LocalLLM(LOCAL_LLM_FILE)
    local_error=''
    if llm.config.get('enabled',False) and llm.config.get('use_for_watch_compile',True):
        try:
            raw=llm.generate_json(prompt)
            proposed=json.loads(raw)
            proposed=validate_watch_proposal(proposed,current,profile)
            return jsonify(ok=True,proposed=proposed,provider='local',model=llm.config.get('model',''),fallback=False,gemini_calls=0)
        except Exception as exc:
            local_error=f"{type(exc).__name__}: {exc}"
            if not llm.config.get('fallback_to_primary',True):
                return jsonify(ok=False,message='Local LLM validation failed',error=local_error,provider='local'),422

    agent=load_agent()
    inter=agent.gemini.interactions.create(model=agent.MODEL,input=prompt)
    raw=(inter.output_text or '').strip().replace('```json','').replace('```','').strip()
    proposed=validate_watch_proposal(json.loads(raw),current,profile)
    return jsonify(ok=True,proposed=proposed,provider='primary',model=agent.MODEL,fallback=bool(local_error),local_error=local_error,gemini_calls=1)

@app.post("/watch/apply")
def apply_watch_proposal():
    data=request.get_json(silent=True) or {}; proposed=data.get("proposed") or {}
    st=write_settings({k:v for k,v in proposed.items() if k in ("watch","limits","inbox_retention_days","source_policy")})
    if isinstance(proposed.get("profile"),dict): write_profile(proposed["profile"])
    return jsonify(ok=True, watch=st.get("watch",{}), limits=st.get("limits",{}), profile=read_profile())

@app.post("/local/index")
def build_local_index():
    """Cheap shared Library Intelligence index: title + abstract + metadata only."""
    try:
        cache=_read_dashboard_cache(); n=0; skipped=0
        for key,d in (cache.get('items') or {}).items():
            title=str(d.get('title') or ''); abstract=str(d.get('abstract') or '')
            tags=d.get('tags') or []; tagtext=' '.join((x.get('tag','') if isinstance(x,dict) else str(x)) for x in tags)
            text=(title+'\n'+abstract+'\n'+tagtext).strip()
            if not key or not text: skipped+=1; continue
            upsert_paper(key,title=title,doi=d.get('doi',''),abstract=abstract,record_text='',analysis_version='1.1-local')
            index_paper(key,text); n+=1
        return jsonify(ok=True,indexed=n,skipped=skipped,gemini_calls=0,message=f'Library Intelligence index: {n} indexed · {skipped} skipped · 0 LLM calls')
    except Exception as exc:return jsonify(ok=False,error=f"{type(exc).__name__}: {exc}"),500

@app.get("/local/related/<item_key>")
def local_related(item_key):
    try: return jsonify(ok=True,related=related_local(item_key,20))
    except Exception as exc: return jsonify(ok=False,error=f"{type(exc).__name__}: {exc}"),500



def _knowledge_sessions_read():
    try:
        if KNOWLEDGE_SESSIONS_FILE.exists():
            x=json.loads(KNOWLEDGE_SESSIONS_FILE.read_text(encoding="utf-8"))
            return x if isinstance(x,dict) else {}
    except Exception: pass
    return {}

def _knowledge_sessions_write(x):
    KNOWLEDGE_SESSIONS_FILE.parent.mkdir(parents=True,exist_ok=True)
    tmp=KNOWLEDGE_SESSIONS_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(x,ensure_ascii=False,indent=2),encoding='utf-8'); tmp.replace(KNOWLEDGE_SESSIONS_FILE)

def _new_knowledge_session(title='New conversation'):
    import uuid
    sid=uuid.uuid4().hex[:12]; now=datetime.now().isoformat(timespec='seconds')
    return sid, {'id':sid,'title':title[:80] or 'New conversation','created_at':now,'updated_at':now,'turns':[]}

@app.get('/knowledge/sessions')
def knowledge_sessions_list():
    db=_knowledge_sessions_read(); rows=[]
    for sid,v in db.items(): rows.append({'id':sid,'title':v.get('title','Conversation'),'updated_at':v.get('updated_at',''),'turns':len(v.get('turns') or [])})
    rows.sort(key=lambda x:x['updated_at'],reverse=True); return jsonify(ok=True,sessions=rows)

@app.post('/knowledge/sessions')
def knowledge_sessions_create():
    data=request.get_json(silent=True) or {}; sid,row=_new_knowledge_session(str(data.get('title') or 'New conversation'))
    db=_knowledge_sessions_read(); db[sid]=row; _knowledge_sessions_write(db); return jsonify(ok=True,session=row)

@app.get('/knowledge/session/<sid>')
def knowledge_session_get(sid):
    row=_knowledge_sessions_read().get(sid)
    return (jsonify(ok=True,session=row) if row else (jsonify(ok=False,message='Conversation not found'),404))

@app.delete('/knowledge/session/<sid>')
def knowledge_session_delete(sid):
    db=_knowledge_sessions_read(); existed=sid in db; db.pop(sid,None); _knowledge_sessions_write(db)
    return jsonify(ok=existed,message='Conversation deleted' if existed else 'Conversation not found'), (200 if existed else 404)

@app.post('/knowledge/find')
def knowledge_find():
    data=request.get_json(silent=True) or {}; question=str(data.get('question') or '').strip()
    if not question:return jsonify(ok=False,message='Question is required'),400
    cache=_read_dashboard_cache(); top_k=min(20,max(1,int(data.get('top_k',10) or 10)))
    rows,stats=knowledge_retrieve(cache,question,limit=top_k,collection_key=str(data.get('collection_key') or '').strip() or None,include_subcollections=bool(data.get('include_subcollections',True)),threshold=float(data.get('threshold',28) or 28))
    sources=[{'n':i+1,'key':r.get('key'),'title':r.get('title'),'year':r.get('year'),'evidence':r.get('evidence'),'relevance':r.get('relevance'),'has_pdf':r.get('has_pdf')} for i,r in enumerate(rows)]
    return jsonify(ok=True,sources=sources,retrieved=len(rows),gemini_calls=0,**stats)

@app.post("/knowledge/enrich-metadata")
def knowledge_enrich_metadata():
    """Fill missing abstract/DOI using metadata APIs only. Never uses an LLM or PDF text.

    Zotero item versions are optimistic-concurrency tokens. We always fetch a fresh item
    immediately before writing and retry a single time on HTTP 412/version conflicts.
    Only missing enrichment fields are merged; collections/tags/other user edits are never
    copied from the dashboard cache back into Zotero.
    """
    data=request.get_json(silent=True) or {}
    collection_key=str(data.get("collection_key") or "").strip() or None
    include_subcollections=bool(data.get("include_subcollections",True))

    def is_version_conflict(exc):
        text=(type(exc).__name__+" "+str(exc)).lower()
        return ("preconditionfailed" in text or "precondition failed" in text or
                "code: 412" in text or "status 412" in text or "http 412" in text)

    def merge_missing_fields(fresh_item, found):
        d=(fresh_item or {}).get("data",{})
        changed=[]
        if not str(d.get("abstractNote") or "").strip() and found.get("abstract"):
            d["abstractNote"]=found["abstract"]
            changed.append("abstract")
        if not str(d.get("DOI") or "").strip() and found.get("doi"):
            d["DOI"]=found["doi"]
            changed.append("DOI")
        return changed

    try:
        cache=_read_dashboard_cache()
        items=list((cache.get("items") or {}).values())
        if collection_key:
            wanted=set()
            def walk(nodes,active=False):
                for n in nodes or []:
                    here=active or n.get("key")==collection_key
                    if here: wanted.add(n.get("key"))
                    walk(n.get("children") or [],here if include_subcollections else False)
            if include_subcollections: walk(cache.get("tree") or [])
            else: wanted={collection_key}
            items=[r for r in items if wanted.intersection(set(r.get("collection_keys") or []))]

        targets=[r for r in items if not str(r.get("abstract") or "").strip() or not str(r.get("doi") or "").strip()]
        agent=load_agent()
        updated=[]; unchanged=[]; failed=[]
        conflict_retried=0; conflict_resolved=0

        for r in targets:
            key=str(r.get("key") or "").strip()
            title=str(r.get("title") or "").strip()
            if not key or not title:
                continue
            try:
                found=enrich_metadata(title,str(r.get("doi") or "").strip())
                if not found:
                    unchanged.append({"key":key,"title":title,"reason":"No confident metadata match"})
                    continue

                # IMPORTANT: never update the stale object represented by dashboard_cache.json.
                fresh=agent.zot.item(key)
                if not fresh or not fresh.get("data"):
                    failed.append({"key":key,"title":title,"reason":"Zotero item not found"})
                    continue
                changed=merge_missing_fields(fresh,found)
                if not changed:
                    unchanged.append({"key":key,"title":title,"reason":"Already complete or nothing missing was available"})
                    continue

                retried=False
                try:
                    agent.zot.update_item(fresh)
                except Exception as exc:
                    if not is_version_conflict(exc):
                        raise
                    # Another Zotero write won the race. Fetch the latest version, merge only
                    # fields that are still missing, then retry ONCE.
                    conflict_retried += 1
                    retried=True
                    fresh=agent.zot.item(key)
                    if not fresh or not fresh.get("data"):
                        raise RuntimeError("Zotero item disappeared while resolving version conflict")
                    changed=merge_missing_fields(fresh,found)
                    if changed:
                        agent.zot.update_item(fresh)  # one retry only
                    else:
                        # The competing write may itself have filled the metadata.
                        unchanged.append({"key":key,"title":title,"reason":"Metadata was filled by another Zotero update during conflict resolution"})
                        conflict_resolved += 1
                        continue

                if retried:
                    conflict_resolved += 1
                updated.append({"key":key,"title":title,"fields":changed,"source":found.get("source"),"match":round(float(found.get("match",0))*100,1),"conflict_retried":retried})
            except Exception as exc:
                failed.append({"key":key,"title":title,"reason":f"{type(exc).__name__}: {exc}"})
                try:
                    with open(BASE_DIR/"agent.log","a",encoding="utf-8") as f:
                        f.write("\nMETADATA ENRICHMENT ITEM ERROR "+key+"\n"+traceback.format_exc()+"\n")
                except Exception:
                    pass

        if updated or conflict_resolved:
            _start_cache_sync()
        already_complete=max(0,len(items)-len(targets))
        msg=(f"Metadata enrichment: {len(items)} papers scanned · {already_complete} complete · {len(targets)} searched · {len(updated)} enriched · "
             f"{conflict_retried} conflict retried · "
             f"{len(failed)} failed · 0 LLM calls")
        return jsonify(ok=True,checked=len(targets),updated=len(updated),already_complete=already_complete,
                       conflict_retried=conflict_retried,conflict_resolved=conflict_resolved,
                       failed=len(failed),items=updated,unchanged=unchanged,failures=failed,message=msg)
    except Exception as exc:
        try:
            with open(BASE_DIR/"agent.log","a",encoding="utf-8") as f:
                f.write("\nMETADATA ENRICHMENT ERROR\n"+traceback.format_exc()+"\n")
        except Exception: pass
        return jsonify(ok=False,error=f"{type(exc).__name__}: {exc}"),500


def _norm_title(v):
    """Canonical title normalization shared by Watch dedupe and paper identity helpers."""
    import re as _re
    return _re.sub(r'[^a-z0-9]+', ' ', str(v or '').lower()).strip()

def _norm_doi(v):
    x=str(v or '').strip().lower()
    for p in ('https://doi.org/','http://doi.org/','doi:'):
        if x.startswith(p): x=x[len(p):]
    return x.strip()

def _paper_identity(row):
    """Stable internal identity. Zotero key is a locator, not the scholarly identity."""
    import re as _re
    doi=_norm_doi(row.get('doi'))
    if doi: return 'doi:'+doi
    blob=' '.join(str(row.get(k) or '') for k in ('url','extra'))
    m=_re.search(r'arxiv(?:\.org/(?:abs|pdf)/|:)\s*([0-9]{4}\.[0-9]{4,5}(?:v\d+)?|[a-z-]+/[0-9]{7}(?:v\d+)?)',blob,_re.I)
    if m: return 'arxiv:'+_re.sub(r'v\d+$','',m.group(1).lower())
    m=_re.search(r'pmcid?\s*[:=]?\s*(PMC\d+|\d+)',blob,_re.I)
    if m: return 'pmid:'+m.group(1).lower()
    title=_norm_title(row.get('title'))
    year=str(row.get('year') or '')
    return 'fp:'+hashlib.sha256((title+'|'+year).encode('utf-8')).hexdigest()[:24]

def _sync_paper_registry(cache=None):
    cache=cache or _read_dashboard_cache(); old=_json_read(PAPER_REGISTRY_FILE,{})
    reg={}
    for key,row in (cache.get('items') or {}).items():
        pid=row.get('paper_id') or _paper_identity(row); prev=old.get(pid,{}); analyses=dict(prev.get('analyses') or {})
        if row.get('card'): analyses['research_card']={'status':'available'}
        else: analyses.pop('research_card',None)
        card=row.get('card') or {}; classifications={k:card.get(k) for k in ('paper_type','research_area','methodology','citation_role') if card.get(k)}
        reg[pid]={'paper_id':pid,'zotero_key':key,'zotero_keys':row.get('zotero_keys') or [key],
                  'duplicate_count':int(row.get('duplicate_count') or 1),'action_key':row.get('action_key') or key,
                  'title':row.get('title',''),'year':row.get('year',''),'doi':_norm_doi(row.get('doi')),
                  'has_pdf':bool(row.get('has_pdf')),'has_card':bool(row.get('card')),'collection_keys':row.get('collection_keys') or [],
                  'classifications':classifications,'analyses':analyses,'updated_at':datetime.now().isoformat(timespec='seconds')}
    _json_write(PAPER_REGISTRY_FILE,reg); return reg

def _comparison_set_id(paper_ids):
    raw='|'.join(sorted(set(paper_ids)))
    return 'cmp_'+hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]

def _clean_compare_text(v):
    import re as _re, html as _html
    x=str(v or '')
    x=_re.sub(r'<br\s*/?>','\n',x,flags=_re.I)
    x=x.replace('\\<','<').replace('\\>','>')
    x=_re.sub(r'^\s*---+\s*$','',x,flags=_re.M)
    x=_re.sub(r'\n{3,}','\n\n',x)
    return _html.unescape(x).strip()

def _comparison_sources(rows):
    return [{'n':i+1,'key':r['key'],'paper_id':r['paper_id'],'title':r['title'],'year':r['year'],
             'evidence':'abstract' if r.get('abstract') else 'metadata'} for i,r in enumerate(rows)]

@app.get('/library/comparisons')
def comparison_history():
    db=_json_read(COMPARISONS_FILE,{})
    rows=[]
    for cid,c in db.items():
        versions=c.get('versions') or []
        rows.append({'id':cid,'label':c.get('label') or 'Paper comparison','paper_count':len(c.get('paper_ids') or []),
                     'updated_at':c.get('updated_at'),'version_count':len(versions),'latest_version':len(versions)})
    rows.sort(key=lambda x:x.get('updated_at') or '',reverse=True)
    return jsonify(ok=True,comparisons=rows)

@app.post('/library/comparison-match')
def comparison_match():
    """Check whether a selected paper set already has a saved comparison. Never calls the Primary LLM."""
    data=request.get_json(silent=True) or {}; keys=list(dict.fromkeys(str(k) for k in (data.get('item_keys') or []) if str(k)))
    if len(keys)<2 or len(keys)>5:return jsonify(ok=True,matched=False)
    cache=_read_dashboard_cache(); reg=_sync_paper_registry(cache)
    paper_ids=[]
    # Derive identity directly from the cached Zotero item. The registry is history/storage,
    # never an availability gate: duplicate identities or registry compaction must not hide a paper.
    for key in keys:
        row=(cache.get('items') or {}).get(key)
        if not row:
            return jsonify(ok=True,matched=False)
        paper_ids.append(_paper_identity(row))
    cid=_comparison_set_id(paper_ids); c=_json_read(COMPARISONS_FILE,{}).get(cid)
    if not c or not (c.get('versions') or []):return jsonify(ok=True,matched=False,id=cid)
    return jsonify(ok=True,matched=True,id=cid,label=c.get('label') or 'Paper comparison',version=len(c.get('versions') or []))

@app.get('/library/comparison/<cid>')
def comparison_get(cid):
    c=_json_read(COMPARISONS_FILE,{}).get(cid)
    if not c:return jsonify(ok=False,message='Comparison not found'),404
    versions=c.get('versions') or []
    if not versions:return jsonify(ok=False,message='Comparison has no saved result'),404
    v=versions[-1]
    return jsonify(ok=True,id=cid,label=c.get('label'),cached=True,version=len(versions),created_at=v.get('created_at'),
                   result=v.get('result') or {},sources=v.get('sources') or [],gemini_calls=0)

@app.post("/knowledge/ask")
def knowledge_ask():
    data=request.get_json(silent=True) or {}; question=str(data.get("question") or "").strip()
    if not question:return jsonify(ok=False,message="Question is required"),400
    try:
        cache=_read_dashboard_cache(); top_k=min(8,max(1,int(data.get("top_k",6) or 6)))
        collection_key=str(data.get("collection_key") or "").strip() or None; include_subcollections=bool(data.get("include_subcollections",True))
        selected_keys=[str(k) for k in (data.get('item_keys') or []) if str(k)]
        work_cache=cache
        if selected_keys:
            keep=set(selected_keys); work_cache=dict(cache); work_cache['items']={k:v for k,v in (cache.get('items') or {}).items() if k in keep}; collection_key=None
        rows,stats=knowledge_retrieve(work_cache,question,limit=top_k,collection_key=collection_key,include_subcollections=include_subcollections)
        if not rows:return jsonify(ok=True,answer="I could not find sufficiently relevant title/abstract evidence in the selected library scope.",sources=[],retrieved=0,gemini_calls=0,**stats)
        # Conversation memory is deliberately compact: prior Q/A excerpts + cited keys/titles only. No old abstracts are resent.
        db=_knowledge_sessions_read(); sid=str(data.get('session_id') or '').strip(); session=db.get(sid)
        if not session:
            sid,session=_new_knowledge_session(question); db[sid]=session
        history=[]
        for t in (session.get('turns') or [])[-4:]:
            history.append({'question':str(t.get('question') or '')[:500],'answer':str(t.get('answer') or '')[:900],'cited_titles':t.get('cited_titles') or [],'cited_keys':t.get('cited_keys') or []})
        agent=load_agent(); prompt=knowledge_build_prompt(question,rows,read_settings().get("output_language","english"),history=history)
        inter=agent.gemini.interactions.create(model=agent.MODEL,input=prompt); answer=(inter.output_text or "").strip()
        sources=[{"n":i+1,"key":r.get("key"),"title":r.get("title"),"year":r.get("year"),"evidence":r.get("evidence"),"relevance":r.get("relevance"),"has_pdf":r.get("has_pdf")} for i,r in enumerate(rows)]
        session['turns'].append({'ts':datetime.now().isoformat(timespec='seconds'),'question':question,'answer':answer,'cited_keys':[r['key'] for r in sources],'cited_titles':[r['title'] for r in sources],'scope':collection_key or 'library'})
        session['turns']=session['turns'][-30:]; session['updated_at']=datetime.now().isoformat(timespec='seconds')
        if len(session['turns'])==1:session['title']=question[:80]
        db[sid]=session; _knowledge_sessions_write(db)
        return jsonify(ok=True,answer=answer,sources=sources,retrieved=len(rows),session_id=sid,gemini_calls=1,**stats)
    except Exception as exc:
        try:
            with open(BASE_DIR/"agent.log","a",encoding="utf-8") as f:f.write("\\nKNOWLEDGE AGENT ERROR\\n"+traceback.format_exc()+"\\n")
        except Exception:pass
        return jsonify(ok=False,error=f"{type(exc).__name__}: {exc}"),500

@app.post('/library/compare')
def library_compare():
    data=request.get_json(silent=True) or {}; keys=list(dict.fromkeys(str(k) for k in (data.get('item_keys') or []) if str(k)))
    force=bool(data.get('force',False))
    if len(keys)<2 or len(keys)>5:return jsonify(ok=False,message='Select 2 to 5 papers'),400
    cache=_read_dashboard_cache(); reg=_sync_paper_registry(cache); rows=[]; missing=[]
    # Compare availability is intentionally minimal: a Library item only needs to exist in the
    # dashboard cache. PDF and Research Card are NOT requirements. Abstract is preferred, with
    # title/year/DOI metadata as the fallback evidence level.
    for key in keys:
        r=(cache.get('items') or {}).get(key)
        if not r:
            missing.append(key); continue
        rows.append({
            'key':key,
            'paper_id':_paper_identity(r),
            'title':r.get('title','Untitled'),
            'year':r.get('year',''),
            'abstract':str(r.get('abstract') or '')[:5000],
            'doi':r.get('doi',''),
            'has_pdf':bool(r.get('has_pdf')),
            'has_card':bool(r.get('card')),
            'evidence':'abstract' if str(r.get('abstract') or '').strip() else 'metadata'
        })
    if len(rows)<2:
        return jsonify(ok=False,message=f'Only {len(rows)} of {len(keys)} selected papers are available in the Library cache',
                       selected=len(keys),available=len(rows),missing_keys=missing,can_sync=True),409
    cid=_comparison_set_id([r['paper_id'] for r in rows]); db=_json_read(COMPARISONS_FILE,{})
    existing=db.get(cid)
    if existing and existing.get('versions') and not force:
        v=existing['versions'][-1]
        return jsonify(ok=True,id=cid,label=existing.get('label'),cached=True,version=len(existing['versions']),result=v.get('result') or {},sources=v.get('sources') or [],gemini_calls=0)
    agent=load_agent(); lang=read_settings().get('output_language','english')
    schema={"label":"3-8 word memorable topic label","overview":"2-4 sentence synthesis","dimensions":[{"name":"Research question","values":["one value per paper"]}],"methods":["one concise paragraph per paper"],"results":["one concise paragraph per paper"],"limitations":["one concise paragraph per paper"],"takeaway":"short practical takeaway"}
    instruction="""Compare the supplied papers using ONLY title, year, DOI, and abstract. Never use or imply PDF/full-text evidence. Return JSON ONLY matching the supplied schema. The label must be a short memorable description of WHAT is being compared, never a date, folder name, or concatenation of titles. Keep prose compact. Do not emit Markdown headings, Markdown tables, HTML, <br>, horizontal rules, or raw LaTeX delimiters. Preserve ordinary scientific symbols such as %, <, >, ±, Greek letters, superscripts where possible. If evidence is absent say "Not stated in abstract". dimensions must contain these names in order: Research question, Data / population, Method, Reported findings, Strengths, Abstract-visible limitations, Best use. Each dimensions.values array must have exactly one string per paper in input order."""
    prompt=instruction+'\nOutput language: '+str(lang)+'\nSCHEMA='+json.dumps(schema,ensure_ascii=False)+'\nPAPERS='+json.dumps(rows,ensure_ascii=False)
    inter=agent.gemini.interactions.create(model=agent.MODEL,input=prompt); raw=(inter.output_text or '').strip().replace('```json','').replace('```','').strip()
    try: result=json.loads(raw)
    except Exception: result={'label':'Focused paper comparison','overview':_clean_compare_text(raw),'dimensions':[],'methods':[],'results':[],'limitations':[],'takeaway':''}
    # sanitize all user-visible strings
    def clean(x):
        if isinstance(x,str):return _clean_compare_text(x)
        if isinstance(x,list):return [clean(v) for v in x]
        if isinstance(x,dict):return {k:clean(v) for k,v in x.items()}
        return x
    result=clean(result); label=(result.get('label') or 'Focused paper comparison')[:100]; sources=_comparison_sources(rows)
    now=datetime.now().isoformat(timespec='seconds'); rec=existing or {'id':cid,'paper_ids':sorted(r['paper_id'] for r in rows),'versions':[]}
    rec['label']=label; rec['updated_at']=now; rec['versions'].append({'created_at':now,'model':getattr(agent,'MODEL',''),'evidence_mode':'abstract','result':result,'sources':sources})
    db[cid]=rec; _json_write(COMPARISONS_FILE,db)
    for r in rows:
        p=reg.get(r['paper_id'])
        if not p:
            # Defensive fallback; comparison itself must never fail merely because registry
            # bookkeeping is stale or compacted.
            p={'paper_id':r['paper_id'],'zotero_key':r['key'],'title':r['title'],'year':r['year'],
               'doi':_norm_doi(r.get('doi')),'has_pdf':bool(r.get('has_pdf')),
               'has_card':bool(r.get('has_card')),'collection_keys':[],'analyses':{},
               'updated_at':datetime.now().isoformat(timespec='seconds')}
            reg[r['paper_id']]=p
        p.setdefault('analyses',{}).setdefault('comparisons',[])
        if cid not in p['analyses']['comparisons']:p['analyses']['comparisons'].append(cid)
    _json_write(PAPER_REGISTRY_FILE,reg)
    return jsonify(ok=True,id=cid,label=label,cached=False,version=len(rec['versions']),result=result,sources=sources,gemini_calls=1)

@app.get("/status")
def status():
    # Dashboard health check must be local-only. Never import the agent or call Zotero here.
    with _lock:
        s = dict(_state)
    llm=PrimaryLLM(PRIMARY_LLM_FILE,BASE_DIR)
    return jsonify(ok=True, service="Literature Agent", version=SERVICE_VERSION, model=llm.model, provider=llm.provider, roots=[], output_language=read_settings()["output_language"], **s)

@app.get("/library")
def library():
    x=_read_dashboard_cache()
    return jsonify(ok=True,total=x.get('total',0),summarized=x.get('summarized',0),pending=x.get('pending',0),no_pdf=x.get('no_pdf',0),cached=True,syncing=_cache_syncing)

@app.post("/scan")
def scan():
    if not start_job("scan"):
        return jsonify(ok=False, message="Agent is already running"), 409
    return jsonify(ok=True, message="Full library scan started")

@app.post("/process")
def process():
    data = request.get_json(silent=True) or {}
    key = data.get("item_key")
    force = bool(data.get("force", False))
    if not key:
        return jsonify(ok=False, message="item_key is required"), 400
    cache=_read_dashboard_cache(); key=_resolve_cached_key(cache,key,prefer_pdf=True)
    mode = "resummarize" if force else "process"
    if not start_job(mode, key, force):
        return jsonify(ok=False, message="Agent is already running"), 409
    return jsonify(ok=True, message=f"{mode} started", item_key=key)

@app.post("/process/manual")
def process_manual():
    """Explicit Paper View analysis. Collection membership is never a prerequisite."""
    data=request.get_json(silent=True) or {}
    requested=str(data.get("item_key") or "").strip()
    force=bool(data.get("force",False))
    if not requested:
        return jsonify(ok=False,message="item_key is required"),400
    cache=_read_dashboard_cache(); canonical=(cache.get('aliases') or {}).get(requested,requested)
    row=(cache.get('items') or {}).get(canonical)
    if not row:
        return jsonify(ok=False,message="Paper is not available in the Library cache. Sync Library and retry."),404
    key=_resolve_cached_key(cache,canonical,prefer_pdf=True)
    # Validate against Zotero now so a stale cache cannot create a silent no-op.
    try:
        agent=load_agent(); item=agent.zot.item(key)
        if not item or not item.get('data'):
            return jsonify(ok=False,message="Zotero item is no longer available. Sync Library and retry."),404
        pdf=agent.find_pdf_attachment(key)
        if not pdf:
            return jsonify(ok=False,message="No PDF attachment is available on the Zotero record selected for analysis."),409
    except Exception as exc:
        return jsonify(ok=False,message=f"Could not validate the paper before analysis: {type(exc).__name__}: {exc}"),500
    mode='resummarize' if force else 'manual_process'
    if not start_job(mode,key,force):
        return jsonify(ok=False,message="Another analysis job is already running"),409
    # Return the resolved Zotero key so the UI can show what actually started.
    return jsonify(ok=True,message="Manual analysis started",item_key=key,requested_key=requested,force=force)


@app.get("/item/<item_key>/card")
def item_card(item_key):
    x=_read_dashboard_cache(); row=(x.get('items') or {}).get(item_key)
    if row: return jsonify(ok=True,item_key=item_key,title=row.get('title','Untitled'),year=row.get('year',''),published=row.get('published',''),date_added=row.get('date_added',''),card=row.get('card'),cached=True)
    return jsonify(ok=False,message='Item not cached yet'),404

@app.get("/item/<item_key>/details")
def item_details(item_key):
    x=_read_dashboard_cache(); row=(x.get('items') or {}).get(item_key)
    if not row: return jsonify(ok=False,message='Item not cached yet'),404
    paths={}
    def walk(nodes,prefix=[]):
        for n in nodes or []:
            pp=prefix+[n.get('name','')]; paths[n.get('key')]=' -> '.join(pp); walk(n.get('children') or [],pp)
    walk(x.get('tree') or [])
    locs=[paths.get(k,k) for k in row.get('collection_keys',[]) if k in paths]
    fields=('key','title','year','published','date_added','doi','url','abstract','summarized','has_pdf')
    return jsonify(ok=True,item={k:row.get(k) for k in fields},locations=locs,has_card=bool(row.get('card')))

@app.get('/item/<item_key>/memory')
def item_memory(item_key):
    """Unified paper memory for the simple Paper View. No LLM calls."""
    x=_read_dashboard_cache(); row=(x.get('items') or {}).get(item_key)
    if not row:return jsonify(ok=False,message='Item not cached yet'),404
    reg=_sync_paper_registry(x); pid=_paper_identity(row); mem=reg.get(pid,{})
    paths={}
    def walk(nodes,prefix=[]):
        for n in nodes or []:
            pp=prefix+[n.get('name','')]; paths[n.get('key')]=' -> '.join(pp); walk(n.get('children') or [],pp)
    walk(x.get('tree') or [])
    locs=[paths.get(k,k) for k in row.get('collection_keys',[]) if k in paths]
    in_inbox=any(p=='00_Inbox' or p.startswith('00_Inbox ->') for p in locs)
    comparisons=[]; cdb=_json_read(COMPARISONS_FILE,{})
    for cid in (mem.get('analyses') or {}).get('comparisons',[]) or []:
        c=cdb.get(cid)
        if c: comparisons.append({'id':cid,'label':c.get('label') or 'Paper comparison','versions':len(c.get('versions') or [])})
    has_pdf=bool(row.get('has_pdf')); has_card=bool(row.get('card'))
    if not has_pdf: next_action={'kind':'find_pdf','label':'Find PDF','reason':'Source PDF is missing.'}
    elif not has_card: next_action={'kind':'analyze','label':'Analyze','reason':'PDF is available but no Research Card exists yet.'}
    elif in_inbox: next_action={'kind':'file','label':'File paper','reason':'Analysis is ready; this paper is still in 00_Inbox.'}
    else: next_action={'kind':'ready','label':'Ready','reason':'PDF, Research Card, and filing are complete.'}
    activity=[]
    if has_card: activity.append({'type':'research_card','label':'Research Card available'})
    if comparisons: activity.append({'type':'comparison','label':f'Used in {len(comparisons)} saved comparison'+('s' if len(comparisons)!=1 else '')})
    if in_inbox: activity.append({'type':'filing','label':'Needs filing from 00_Inbox'})
    return jsonify(ok=True,paper_id=pid,item={
        'key':item_key,'title':row.get('title','Untitled'),'year':row.get('year',''),'published':row.get('published',''),
        'date_added':row.get('date_added',''),'doi':row.get('doi',''),'url':row.get('url',''),'abstract':row.get('abstract',''),
        'has_pdf':has_pdf,'has_card':has_card,'locations':locs,'in_inbox':in_inbox,
        'classifications':mem.get('classifications') or {},'card':row.get('card'),'zotero_keys':row.get('zotero_keys') or [item_key],'duplicate_count':int(row.get('duplicate_count') or 1)
    },activity=activity,comparisons=comparisons,next_action=next_action)

@app.get('/library/needs-filing')
def library_needs_filing():
    x=_read_dashboard_cache();
    try: inbox_key=_zotero_role_map(load_agent()).get('inbox')
    except Exception: inbox_key=None
    inbox={inbox_key} if inbox_key else set(); rows=[]
    for key,r in (x.get('items') or {}).items():
        if inbox.intersection(set(r.get('collection_keys') or [])):
            rows.append({'key':key,'title':r.get('title','Untitled'),'year':r.get('year',''),'has_pdf':bool(r.get('has_pdf')),'summarized':bool(r.get('summarized'))})
    rows.sort(key=lambda r:r['title'].lower()); return jsonify(ok=True,count=len(rows),items=rows)

@app.get("/collections")
def collections():
    try:
        agent=load_agent(); return jsonify(ok=True,collections=collection_catalog(agent))
    except Exception as exc: return jsonify(ok=False,error=f"{type(exc).__name__}: {exc}"),500

@app.get("/library/tree")
def library_tree():
    x=_read_dashboard_cache()
    return jsonify(ok=True,tree=x.get('tree',[]),cached=True,syncing=_cache_syncing,updated_at=x.get('updated_at'))

@app.get("/item/<item_key>/locations")
def item_locations(item_key):
    x=_read_dashboard_cache(); item_key=(x.get('aliases') or {}).get(item_key,item_key); row=(x.get('items') or {}).get(item_key)
    if not row: return jsonify(ok=False,message='Item not cached yet'),404
    keys=row.get('collection_keys',[])
    try:
        agent=load_agent(); inbox_key=_zotero_role_map(agent).get('inbox')
    except Exception:
        inbox_key=None
    return jsonify(ok=True,item_key=item_key,collection_keys=keys,in_inbox=bool(inbox_key and inbox_key in keys),cached=True)

@app.post("/filing")
def filing():
    data=request.get_json(silent=True) or {}; item_key=data.get("item_key"); selected=list(dict.fromkeys(data.get("collection_keys") or [])); remove_inbox=bool(data.get("remove_inbox",False))
    if not item_key: return jsonify(ok=False,message="item_key is required"),400
    try:
        cache=_read_dashboard_cache(); canonical=(cache.get('aliases') or {}).get(item_key,item_key); logical=(cache.get('items') or {}).get(canonical)
        agent=load_agent(); valid={x["key"]:x for x in collection_catalog(agent)}; selected=[k for k in selected if k in valid]
        inbox_key=_zotero_role_map(agent).get('inbox')
        if remove_inbox and inbox_key:
            selected=[k for k in selected if k!=inbox_key]
            if not selected:
                return jsonify(ok=False,message="Cannot remove the configured Inbox until at least one destination is selected."),409
        # Zotero is authoritative. Preserve every unmanaged collection and mutate only the
        # managed role tree, using fresh-read/version-aware writes to avoid 412 conflicts.
        zotero_keys=(logical or {}).get('zotero_keys') or [item_key]
        updated=[]; managed=set(valid)
        for zk in zotero_keys:
            def mutate(data):
                current=list(data.get('collections',[]) or []); unmanaged=[k for k in current if k not in managed]
                target=list(dict.fromkeys(unmanaged+selected))
                if target==current: return False
                data['collections']=target; return True
            _update_zotero_item_fresh(agent,zk,mutate,retries=1); updated.append(zk)
        if not updated: return jsonify(ok=False,message="Item not found"),404
        # Local-first consistency: UI changes now; one debounced full reconciliation follows later.
        _patch_cached_locations(canonical,selected)
        _schedule_cache_reconcile()
        return jsonify(ok=True,message="Paper locations updated",collection_keys=selected,updated_records=len(updated),cache_updated=True,reconcile_scheduled=True)
    except Exception as exc: return jsonify(ok=False,error=f"{type(exc).__name__}: {exc}"),500

@app.post("/collections/create")
def create_collection():
    data=request.get_json(silent=True) or {}; name=str(data.get("name") or "").strip(); parent=data.get("parent_key") or None
    if not name: return jsonify(ok=False,message="Collection name is required"),400
    try:
        agent=load_agent(); valid={x["key"]:x for x in collection_catalog(agent)}
        if parent and parent not in valid: return jsonify(ok=False,message="Parent must be inside a configured Zotero role collection"),400
        if not parent and not name[0].isdigit(): return jsonify(ok=False,message="New top-level collection must start with a digit"),400
        payload={"name":name};
        if parent: payload["parentCollection"]=parent
        result=agent.zot.create_collections([payload]); _start_cache_sync(); return jsonify(ok=True,message=f"Created collection: {name}",result=result)
    except Exception as exc: return jsonify(ok=False,error=f"{type(exc).__name__}: {exc}"),500

@app.post("/collections/create-path")
def create_collection_path():
    data=request.get_json(silent=True) or {}; raw=str(data.get("path") or "").strip(); item_key=data.get("item_key"); parts=[p.strip() for p in raw.replace("→","->").split("->") if p.strip()]
    if not parts or not parts[0][0].isdigit(): return jsonify(ok=False,message="Recommended path must begin with a numeric root"),400
    try:
        agent=load_agent()
        for _ in range(len(parts)+2):
            catalog=collection_catalog(agent); exact={x["path"]:x for x in catalog}; current=parts[0]
            if current not in exact:
                # Saved Zotero collection keys define roles. Prefix matching is only a legacy
                # bridge for old Research Cards created before role mapping existed.
                import re as _re
                m=_re.match(r"^(\d{2})(?:[_\- &]|$)", current); prefix=m.group(1) if m else ""
                role=next((r for r,p in ZOTERO_ROLE_PREFIX.items() if p==prefix),None)
                role_key=_zotero_role_map(agent).get(role) if role else None
                root=next((x for x in catalog if x.get('key')==role_key),None)
                if root:
                    current=root['name']; parts[0]=current
                else:
                    return jsonify(ok=False,message=f"Recommended root is not mapped in Settings → Zotero Integration: {parts[0]}"),400
            parent=exact[current]["key"]; created=False
            for name in parts[1:]:
                nxt=current+" -> "+name
                if nxt in exact: parent=exact[nxt]["key"]; current=nxt; continue
                agent.zot.create_collections([{"name":name,"parentCollection":parent}]); created=True; break
            if not created:
                # IMPORTANT: folder creation has no paper-placement side effect.  The browser
                # receives the real collection key, adds it to locationSet, and /filing is the
                # single transaction that changes paper membership.  This prevents a later
                # Save locations call from accidentally undoing a just-created destination.
                return jsonify(ok=True,message=f"Ready: {current}",key=parent,path=current)
        return jsonify(ok=False,message="Could not create path"),500
    except Exception as exc: return jsonify(ok=False,error=f"{type(exc).__name__}: {exc}"),500



# ============================================================
# COLLECTION RENAME + MANUAL PDF RECOVERY
# ============================================================

@app.post('/collections/rename')
def rename_collection():
    data=request.get_json(silent=True) or {}; key=str(data.get('key') or ''); name=str(data.get('name') or '').strip()
    if not key or not name: return jsonify(ok=False,message='Collection key and new name are required'),400
    try:
        agent=load_agent(); valid={x['key']:x for x in collection_catalog(agent)}
        if key not in valid: return jsonify(ok=False,message='Collection is outside the managed numeric roots'),404
        # Zotero collections are updated with their collection JSON object.
        coll=next((c for c in agent.get_all_collections() if c.get('data',{}).get('key')==key),None)
        if not coll: return jsonify(ok=False,message='Collection not found'),404
        coll['data']['name']=name
        agent.zot.update_collection(coll)
        _patch_cached_collection_name(key,name)
        _schedule_cache_reconcile()
        return jsonify(ok=True,message=f'Renamed to {name}')
    except Exception as exc: return jsonify(ok=False,error=f'{type(exc).__name__}: {exc}'),500

def _openalex_pdf_url(meta):
    doi=str(meta.get('doi') or '').strip()
    if not doi: return ''
    try:
        url='https://api.openalex.org/works/https://doi.org/'+urllib.parse.quote(doi,safe='')
        data=json.loads(_http_text(url,timeout=20))
        for loc in [data.get('best_oa_location') or {}]+list(data.get('locations') or []):
            u=str(loc.get('pdf_url') or '')
            if u.startswith('http'): return u
    except Exception: pass
    return ''

def _europepmc_pdf_url(meta):
    doi=str(meta.get('doi') or '').strip(); title=str(meta.get('title') or '').strip()
    if not doi and not title: return ''
    try:
        q=('DOI:'+doi) if doi else ('TITLE:"'+title.replace('"','')+'"')
        params=urllib.parse.urlencode({'query':q,'format':'json','pageSize':5})
        data=json.loads(_http_text('https://www.ebi.ac.uk/europepmc/webservices/rest/search?'+params,timeout=20))
        for r in data.get('resultList',{}).get('result',[]):
            pmcid=str(r.get('pmcid') or '')
            if pmcid: return 'https://www.ebi.ac.uk/europepmc/webservices/rest/'+pmcid+'/fullTextPDF'
    except Exception: pass
    return ''

def _arxiv_pdf_url_from_meta(meta):
    import re
    blob=' '.join(str(meta.get(k) or '') for k in ('url','extra','doi'))
    m=re.search(r'(?:arxiv(?:\.org/(?:abs|pdf)/|:))\s*([0-9]{4}\.[0-9]{4,5}(?:v\d+)?|[a-z-]+/[0-9]{7}(?:v\d+)?)',blob,re.I)
    return ('https://export.arxiv.org/pdf/'+m.group(1)+'.pdf') if m else ''

def _download_pdf_urls(meta, urls):
    return pdf_download_urls(meta, urls, _download_dir())

def _recover_one_pdf(item_key):
    cache=_read_dashboard_cache(); meta=(cache.get('items') or {}).get(item_key)
    if not meta: return {'key':item_key,'ok':False,'status':'not-cached','message':'Item is not in the local Library cache'}
    if meta.get('has_pdf'): return {'key':item_key,'ok':True,'status':'already-has-pdf','message':'PDF already attached'}
    contact=str((read_settings().get('pdf_recovery') or {}).get('unpaywall_email') or '').strip()
    candidates=pdf_oa_candidates(meta,contact)
    path,provenance=pdf_download_candidates(meta,candidates,_download_dir())
    source=provenance.get('source','') if provenance else ''
    if not path: return {'key':item_key,'title':meta.get('title'),'ok':False,'status':'not-found','message':'No validated legal open-access PDF found','doi_url':('https://doi.org/'+str(meta.get('doi') or '')) if meta.get('doi') else '', 'checked_sources':list(dict.fromkeys(c.get('source') for c in candidates))}
    try:
        agent=load_agent(); attached, attach_detail=_attach_local_pdf_best_effort(agent,item_key,path)
        if not attached: return {'key':item_key,'title':meta.get('title'),'ok':False,'status':'attach-failed','message':f'PDF available from {source}, but Zotero attachment failed: {attach_detail}','path':str(path)}
        auto_cfg=read_settings().get('automation') or {}
        _patch_cached_paper_state(item_key,has_pdf=True)
        _schedule_cache_reconcile()
        if auto_cfg.get('auto_summary_after_accept',True): _auto_analyze_after_accept(item_key)
        return {'key':item_key,'title':meta.get('title'),'ok':True,'status':'recovered','message':f'Recovered via {source}','path':str(path),'pdf_source':source,'pdf_version':(provenance or {}).get('version',''),'pdf_license':(provenance or {}).get('license',''),'summary_queued':bool(auto_cfg.get('auto_summary_after_accept',True))}
    except Exception as exc:
        return {'key':item_key,'title':meta.get('title'),'ok':False,'status':'error','message':f'{type(exc).__name__}: {exc}','path':str(path)}

@app.get('/pdf-recovery/missing')
def pdf_recovery_missing():
    x=_read_dashboard_cache(); items=x.get('items') or {}; rows=[]
    paths={}
    def walk(nodes,prefix=[]):
        for n in nodes:
            pp=prefix+[n.get('name','')]; paths[n.get('key')]=' -> '.join(pp); walk(n.get('children') or [],pp)
    walk(x.get('tree') or [])
    for key,r in items.items():
        if r.get('has_pdf'): continue
        locs=[paths.get(k,k) for k in r.get('collection_keys',[]) if k in paths]
        rows.append({'key':key,'title':r.get('title','Untitled'),'year':r.get('year',''),'doi':r.get('doi',''),'summarized':bool(r.get('summarized')),'has_card':bool(r.get('card')),'locations':locs})
    rows.sort(key=lambda z:(z['locations'][0] if z['locations'] else '',z['title'].lower()))
    return jsonify(ok=True,items=rows,count=len(rows))

@app.post('/pdf-recovery/run')
def pdf_recovery_run():
    data=request.get_json(silent=True) or {}; keys=list(dict.fromkeys(data.get('item_keys') or []))
    if not keys: return jsonify(ok=False,message='Select at least one paper'),400
    results=[_recover_one_pdf(k) for k in keys]
    _schedule_cache_reconcile()
    return jsonify(ok=True,results=results,recovered=sum(1 for r in results if r.get('status')=='recovered'),total=len(results))

# ============================================================
# WATCH AGENT V1 - official APIs only, low-frequency/batched
# ============================================================

def _json_read(path, default):
    try:
        if path.exists(): return json.loads(path.read_text(encoding='utf-8'))
    except Exception: pass
    return default

def _json_write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp=path.with_suffix('.tmp'); tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8'); tmp.replace(path)

def _candidate_id(source, external_id, title):
    return hashlib.sha1(f'{source}|{external_id}|{_norm_title(title)}'.encode()).hexdigest()[:16]

DEFAULT_SOURCE_POLICY = {
    "scope": "balanced",
    "preferred_journals": [],
    "excluded_journals": [],
    "allow_preprints": True,
    "require_open_access": False,
    "pubmed": {"use_title_abstract": True, "use_mesh": True, "publication_types": [], "date_window_days": 0},
    "europe_pmc": {"date_window_days": 0},
    "arxiv": {"categories": ["cs.AI", "cs.LG", "q-bio"], "date_window_days": 0},
    "ranking": {"preferred_journal_bonus": 6, "library_affinity_weight": 12}
}

def _source_policy(cfg=None):
    cfg=cfg or read_settings(); return _deep_merge(DEFAULT_SOURCE_POLICY, cfg.get('source_policy') or {})

def _iso_date_only(value):
    dt=_parse_iso(value) if value else None
    return dt.strftime('%Y/%m/%d') if dt else ''

def _journal_adjust(row, policy):
    j=(row.get('journal') or '').strip().lower()
    excluded={x.strip().lower() for x in policy.get('excluded_journals',[]) if str(x).strip()}
    preferred={x.strip().lower() for x in policy.get('preferred_journals',[]) if str(x).strip()}
    if j and j in excluded: return None
    bonus=int((policy.get('ranking') or {}).get('preferred_journal_bonus',6)) if j and j in preferred else 0
    return bonus

def _library_affinity(row, cache):
    import re, math
    stop={'the','and','for','with','from','using','into','this','that','are','was','were','study','paper','method','methods'}
    def toks(x): return {w for w in re.findall(r'[a-z][a-z0-9-]{2,}',(x or '').lower()) if w not in stop}
    a=toks((row.get('title') or '')+' '+(row.get('abstract') or '')[:1500])
    if not a:return 0.0
    best=0.0
    for x in list((cache.get('items') or {}).values())[:500]:
        b=toks(x.get('title',''))
        if not b:continue
        best=max(best,len(a&b)/math.sqrt(len(a)*len(b)))
    return best

def _watch_query(cfg, category):
    row=(cfg.get('watch') or {}).get(category) or {}
    q=str(row.get('query') or '').strip()
    if q: return q
    return ''

def _local_keyword_score(candidate, query):
    import re
    words={w for w in re.findall(r'[A-Za-z][A-Za-z0-9_-]{2,}',query.lower()) if w not in {'and','the','with','from','that','this','papers','research','methods','review'}}
    if not words: return 50
    text=(candidate.get('title','')+' '+candidate.get('abstract','')).lower(); hit=sum(1 for w in words if w in text)
    return min(100, int(35+65*hit/max(1,min(len(words),8))))

def _screen_candidates_with_llm(rows, category, threshold, instructions=''):
    if not rows: return []
    agent=load_agent()
    payload=[{'id':r['id'],'title':r['title'],'abstract':r.get('abstract','')[:2500],'year':r.get('year'),'source':r.get('source')} for r in rows]
    prompt=f'''{read_prompts()['watch_screen']}
{read_prompts()['light_analysis']}

Score literature candidates for a researcher's personal library. Category: {category}.\nSHARED USER-EDITABLE TAXONOMY:\n{role_prompts_combined(BASE_DIR)}\nUser instructions: {instructions}\nScore 0-100 primarily for RELEVANCE to the user's requested topic, then evidence/methodological value/novelty/reproducibility/recency. Do not reward prestige if irrelevant. Return JSON only: array of objects {{id,score,reason,classification,recommended_collections}}. classification must be one of research, methods, review. recommended_collections is an array of 0-2 concise suggested collection paths based ONLY on title/abstract/metadata; do not read or assume full text. One concise reason.\nCANDIDATES={json.dumps(payload,ensure_ascii=False)}'''
    inter=agent.gemini.interactions.create(model=agent.MODEL,input=prompt)
    raw=(inter.output_text or '').strip().replace('```json','').replace('```','').strip(); scored=json.loads(raw)
    by={x['id']:x for x in rows}; out=[]
    for s in scored if isinstance(scored,list) else []:
        cid=str(s.get('id',''))
        if cid not in by: continue
        r=dict(by[cid]); r['score']=int(float(s.get('score',0) or 0)); r['reason']=str(s.get('reason','')); r['light_classification']=str(s.get('classification') or category); r['recommended_collections']=[str(x) for x in (s.get('recommended_collections') or [])[:2]]
        if r['score']>=threshold: out.append(r)
    return sorted(out,key=lambda x:x['score'],reverse=True)

def _frequency_days(freq):
    return {'daily':1,'weekly':7,'biweekly':14,'monthly':30}.get(str(freq or 'daily').lower(),1)

def _category_due(state, category, cfg, force=False):
    if force: return True
    last=_parse_iso(((state.get('categories') or {}).get(category) or {}).get('last_checked'))
    if not last: return True
    days=_frequency_days(((cfg.get('watch') or {}).get(category) or {}).get('frequency','daily'))
    return datetime.now() >= last + timedelta(days=days)

def _next_check(last_checked, frequency):
    last=_parse_iso(last_checked)
    if not last: return 'due now'
    return (last+timedelta(days=_frequency_days(frequency))).isoformat(timespec='seconds')

def run_watch_once(force=False):
    cfg=read_settings(); policy=_source_policy(cfg); saved=_json_read(WATCH_CANDIDATES_FILE,[]); state=_json_read(WATCH_STATE_FILE,{})
    state.setdefault('categories',{})
    # Keep accepted/rejected/maybe records too: they are the durable dedupe + feedback history.
    existing_dois={(x.get('doi') or '').lower() for x in saved if x.get('doi')}
    existing_titles={_norm_title(x.get('title')) for x in saved if x.get('title')}
    rejected_titles={_norm_title(x.get('title')) for x in saved if x.get('status')=='rejected'}
    try:
        cache=_read_dashboard_cache(); library_dois=set(); library_titles={_norm_title(x.get('title')) for x in (cache.get('items') or {}).values()}
        for x in (cache.get('items') or {}).values():
            if x.get('doi'): library_dois.add(x['doi'].lower())
    except Exception: library_dois=set(); library_titles=set()
    all_new=[]; source_calls=0; ran=[]; skipped=[]
    for category in ('research','methods','review'):
        wc=(cfg.get('watch') or {}).get(category) or {}
        if not wc.get('enabled',True): skipped.append(category); continue
        if not _category_due(state,category,cfg,force): skipped.append(category); continue
        query=_watch_query(cfg,category)
        if not query: skipped.append(category); continue
        raw=[]; cat_calls=0; last_checked=((state.get('categories') or {}).get(category) or {}).get('last_checked')
        for src in wc.get('sources',[]):
            try:
                n=min(int(wc.get('max_candidates',25)),40)
                if src=='pubmed': raw+=_pubmed_search(query,n,policy,last_checked,category); cat_calls+=2
                elif src=='europe_pmc': raw+=_europe_pmc_search(query,n,policy,last_checked,category); cat_calls+=1
                elif src=='arxiv' and policy.get('allow_preprints',True): raw+=_arxiv_search(query,n,policy,last_checked,category); cat_calls+=1
            except Exception:
                try:
                    with open(BASE_DIR/'agent.log','a',encoding='utf-8') as f:f.write('\nWATCH SOURCE ERROR '+src+'\n'+traceback.format_exc()+'\n')
                except Exception: pass
        source_calls += cat_calls
        ded=[]; seen=set()
        for r in raw:
            doi=(r.get('doi') or '').lower(); nt=_norm_title(r.get('title')); key=('doi',doi) if doi else ('title',nt)
            if not nt or key in seen or doi in library_dois or nt in library_titles or doi in existing_dois or nt in existing_titles: continue
            seen.add(key); r['id']=_candidate_id(r['source'],r.get('external_id',''),r['title']); r['category']=category; r['local_score']=_local_keyword_score(r,query)
            jb=_journal_adjust(r,policy)
            if jb is None: continue
            affinity=_library_affinity(r,cache); r['library_affinity']=round(affinity,3); r['journal_bonus']=jb
            r['local_score']=min(100,int(r['local_score']+jb+affinity*int((policy.get('ranking') or {}).get('library_affinity_weight',12))))
            # Lightweight negative-feedback penalty for titles sharing words with rejected papers.
            if rejected_titles:
                words=set(nt.split()); overlap=max((len(words & set(t.split()))/max(1,len(words)) for t in rejected_titles), default=0)
                r['local_score']=max(0,int(r['local_score']-20*overlap))
            ded.append(r)
        ded.sort(key=lambda x:x['local_score'],reverse=True)
        max_llm=min(int((cfg.get('limits') or {}).get('max_llm_screenings_per_day',30)),len(ded)); ded=ded[:max_llm]
        threshold=int(wc.get('threshold',80)); scored=_screen_candidates_with_llm(ded,category,threshold,query)
        found=scored[:int(wc.get('max_candidates',25))]; all_new+=found
        now=datetime.now().isoformat(timespec='seconds')
        state['categories'][category]={'last_checked':now,'next_check':_next_check(now,wc.get('frequency','daily')),'frequency':wc.get('frequency','daily'),'new_candidates':len(found),'source_requests':cat_calls}
        ran.append(category)
    # Preserve full history, updating/inserting candidates by id.
    byid={x.get('id'):x for x in saved if x.get('id')}
    for x in all_new:
        x['status']='candidate'; x['found_at']=datetime.now().isoformat(timespec='seconds'); byid[x['id']]=x
    result=list(byid.values())
    result.sort(key=lambda x:(x.get('status')=='candidate',x.get('score',0),x.get('found_at','')),reverse=True)
    _json_write(WATCH_CANDIDATES_FILE,result)
    state.update({'last_run':datetime.now().isoformat(timespec='seconds'),'source_requests':source_calls,'new_candidates':len(all_new),'ran_categories':ran,'skipped_categories':skipped})
    _json_write(WATCH_STATE_FILE,state)
    return {'candidates':[x for x in result if x.get('status')=='candidate'],'new_candidates':len(all_new),'source_requests':source_calls,'ran_categories':ran,'skipped_categories':skipped,'state':state}

def _find_inbox_key(agent):
    """Inbox is a semantic Zotero role, never a hard-coded display name."""
    return _zotero_role_map(agent).get('inbox')

def _creator_from_name(name):
    parts=(name or '').strip().split()
    if not parts:return None
    if len(parts)==1:return {'creatorType':'author','lastName':parts[0],'firstName':''}
    return {'creatorType':'author','lastName':parts[-1],'firstName':' '.join(parts[:-1])}

@app.post('/watch/run')
def watch_run_api():
    try:
        data=request.get_json(silent=True) or {}; r=run_watch_once(force=bool(data.get('force',False))); return jsonify(ok=True,message=f"Watch complete: {r['new_candidates']} new candidates; {r['source_requests']} source requests",**r)
    except Exception as exc:
        return jsonify(ok=False,error=f'{type(exc).__name__}: {exc}'),500

@app.get('/watch/candidates')
def watch_candidates_api():
    rows=_json_read(WATCH_CANDIDATES_FILE,[])
    return jsonify(ok=True,candidates=[x for x in rows if x.get('status','candidate')=='candidate'],maybe=[x for x in rows if x.get('status')=='maybe'],history=[x for x in rows if x.get('status') in ('accepted','rejected')],state=_json_read(WATCH_STATE_FILE,{}))

@app.post('/watch/candidate/<cid>/reject')
def watch_reject_api(cid):
    rows=_json_read(WATCH_CANDIDATES_FILE,[]); found=False
    for x in rows:
        if x.get('id')==cid: x['status']='rejected'; found=True
    _json_write(WATCH_CANDIDATES_FILE,rows)
    return jsonify(ok=found,message='Candidate rejected' if found else 'Candidate not found'), (200 if found else 404)

@app.post('/watch/candidate/<cid>/restore')
def watch_restore_api(cid):
    rows=_json_read(WATCH_CANDIDATES_FILE,[]); found=False
    for x in rows:
        if x.get('id')==cid: x['status']='candidate'; x['restored_at']=datetime.now().isoformat(timespec='seconds'); found=True
    _json_write(WATCH_CANDIDATES_FILE,rows)
    return jsonify(ok=found,message='Candidate restored' if found else 'Candidate not found'), (200 if found else 404)

@app.post('/watch/candidate/<cid>/maybe')
def watch_maybe_api(cid):
    rows=_json_read(WATCH_CANDIDATES_FILE,[]); found=False
    for x in rows:
        if x.get('id')==cid: x['status']='maybe'; x['maybe_at']=datetime.now().isoformat(timespec='seconds'); found=True
    _json_write(WATCH_CANDIDATES_FILE,rows)
    return jsonify(ok=found,message='Candidate saved for later' if found else 'Candidate not found'), (200 if found else 404)

def _candidate_pdf_urls(cand):
    """Only deterministic/public PDF endpoints. No HTML scraping."""
    urls=[]; src=cand.get('source'); eid=str(cand.get('external_id') or '')
    if src=='arxiv' and eid:
        urls.append('https://export.arxiv.org/pdf/'+eid+'.pdf')
    # Europe PMC OA: PMCID can be fetched from its public REST full-text endpoint.
    if src=='europe_pmc' and eid.upper().startswith('PMC'):
        urls.append('https://www.ebi.ac.uk/europepmc/webservices/rest/'+eid+'/fullTextPDF')
    return urls

def _download_candidate_pdf(cand):
    meta={'title':cand.get('title',''),'doi':cand.get('doi',''),'url':cand.get('url',''),'extra':cand.get('extra','')}
    contact=str((read_settings().get('pdf_recovery') or {}).get('unpaywall_email') or '').strip()
    candidates=[]
    for u in _candidate_pdf_urls(cand): candidates.append({'source':str(cand.get('source') or 'source API'),'url':u,'version':'','license':''})
    candidates += pdf_oa_candidates(meta,contact)
    path,_=pdf_download_candidates(meta,candidates,_download_dir())
    return path

def _attach_local_pdf_best_effort(agent,parent_key,path):
    return pdf_attach_local(agent,parent_key,path,BASE_DIR/'agent.log')

def _pdf_text_token_estimate(path):
    return pdf_token_estimate(path)

def _long_paper_threshold():
    """80th percentile of known accepted PDF token estimates; conservative fallback until enough history exists."""
    rows=_json_read(WATCH_CANDIDATES_FILE,[])
    vals=sorted(int(x.get('pdf_token_estimate') or 0) for x in rows if int(x.get('pdf_token_estimate') or 0)>0)
    if len(vals)>=5:
        pct=float((read_settings().get('automation') or {}).get('long_paper_percentile',80))/100.0
        i=min(len(vals)-1, max(0, int((len(vals)-1)*pct)))
        return max(30000, vals[i])
    return 60000

def _auto_analyze_after_accept(item_key):
    """Immediate full analysis after Accept when a PDF was attached; periodic scan remains a fallback."""
    if not item_key: return
    def worker():
        for _ in range(60):
            with _lock:
                busy=_state.get('running',False)
            if not busy:
                if start_job('process',item_key,False): return
            time.sleep(2)
    threading.Thread(target=worker,daemon=True).start()

@app.post('/watch/candidate/<cid>/accept')
def watch_accept_api(cid):
    rows=_json_read(WATCH_CANDIDATES_FILE,[]); cand=next((x for x in rows if x.get('id')==cid),None)
    if not cand:return jsonify(ok=False,message='Candidate not found'),404
    data=request.get_json(silent=True) or {}; confirmed=bool(data.get('confirm_long',False))
    try:
        # Download first so the warning is based on the actual locally extracted text, not page/file-size guesses.
        auto_cfg=read_settings().get('automation') or {}
        pdf=_download_candidate_pdf(cand) if auto_cfg.get('auto_download_pdf',True) else None
        token_est=_pdf_text_token_estimate(pdf) if pdf else 0
        threshold=_long_paper_threshold()
        cand['pdf_token_estimate']=token_est
        cand['long_paper_threshold']=threshold
        _json_write(WATCH_CANDIDATES_FILE,rows)
        if pdf and token_est>=threshold and not confirmed:
            return jsonify(ok=False,warning='long_paper',requires_confirmation=True,
                message=f'Long paper warning: estimated {token_est/1000:.0f}K text tokens. This is in roughly the longest 20% of papers and automatic full Summary may use substantially more tokens.',
                estimated_tokens=token_est,threshold_tokens=threshold),409

        agent=load_agent(); inbox=_find_inbox_key(agent)
        if not inbox:return jsonify(ok=False,message='00_Inbox not found'),400
        t=agent.zot.item_template('journalArticle'); t['title']=cand.get('title','Untitled'); t['date']=cand.get('year',''); t['DOI']=cand.get('doi',''); t['url']=cand.get('url',''); t['abstractNote']=cand.get('abstract',''); t['collections']=[inbox]
        creators=[_creator_from_name(n) for n in cand.get('authors',[])]; t['creators']=[x for x in creators if x]
        tags=[{'tag':'watch-agent'},{'tag':'watch-'+cand.get('category','candidate')}]
        for rp in (cand.get('recommended_collections') or [])[:3]: tags.append({'tag':'agent-rec:'+str(rp)[:80]})
        t['tags']=tags
        result=agent.zot.create_items([t])
        parent_key=''
        if isinstance(result,dict):
            succ=result.get('successful') or {}; first=next(iter(succ.values()),{}) if isinstance(succ,dict) else {}; parent_key=first.get('key','') if isinstance(first,dict) else ''
        attached, attach_detail=_attach_local_pdf_best_effort(agent,parent_key,pdf) if parent_key and pdf else (False, 'no PDF or parent key')
        cand['status']='accepted'; cand['accepted_at']=datetime.now().isoformat(timespec='seconds');
        # Search priority is query-dependent and must not become permanent paper metadata.
        for _k in ('score','local_score','library_affinity','journal_bonus','reason'): cand.pop(_k,None)
        cand['local_pdf']=str(pdf) if pdf else ''; cand['pdf_attached']=attached
        _json_write(WATCH_CANDIDATES_FILE,rows)
        _patch_cached_new_watch_item(parent_key,cand,inbox,bool(pdf and attached))
        _schedule_cache_reconcile()
        if pdf and attached and parent_key and auto_cfg.get('auto_summary_after_accept',True): _auto_analyze_after_accept(parent_key)
        suffix=(' · PDF downloaded'+(' and attached · Summary queued' if attached else ' to downloads/') if pdf else ' · no public PDF found; periodic scan will analyze after a PDF is available')
        return jsonify(ok=True,message='Accepted to 00_Inbox'+suffix,result=result,pdf=str(pdf) if pdf else None,attached=attached,summary_queued=bool(pdf and attached and parent_key),estimated_tokens=token_est,attachment_detail=attach_detail)
    except Exception as exc:return jsonify(ok=False,error=f'{type(exc).__name__}: {exc}'),500

@app.post('/watch/batch')
def watch_batch_api():
    data=request.get_json(silent=True) or {}; ids=list(dict.fromkeys(data.get('ids') or [])); action=str(data.get('action') or '')
    if action not in ('accept','maybe','reject') or not ids:return jsonify(ok=False,message='ids and valid action required'),400
    done=[]; errors=[]
    for cid in ids:
        try:
            if action=='accept':
                with app.test_request_context(): resp=watch_accept_api(cid)
            elif action=='maybe':
                with app.test_request_context(): resp=watch_maybe_api(cid)
            else:
                with app.test_request_context(): resp=watch_reject_api(cid)
            done.append(cid)
        except Exception as e: errors.append({'id':cid,'error':str(e)})
    return jsonify(ok=not errors,processed=len(done),errors=errors,message=f'{action.title()}: {len(done)} processed')

@app.post("/shutdown")
def shutdown_server():
    def _exit_later():
        time.sleep(0.35)
        os._exit(0)
    threading.Thread(target=_exit_later, daemon=True).start()
    return jsonify(ok=True, message="Literature Agent server shutting down")

def _summary_text_for_item(agent, item_key):
    for child in agent.get_children(item_key):
        d = child.get("data", {})
        if d.get("itemType") != "note":
            continue
        note = d.get("note", "")
        if agent.AI_NOTE_MARKER.lower() in note.lower():
            return strip_html(note)
    return ""

@app.get("/related/<item_key>")
def related_papers(item_key):
    """Entire-Library related-paper retrieval: local recall first, one Primary-LLM relation pass second.

    Never requires PDF or Research Card. Saved results are reused while the library evidence
    fingerprint is unchanged; ?refresh=1 forces a fresh relation pass.
    """
    try:
        cache=_read_dashboard_cache(); items=cache.get('items') or {}; aliases=cache.get('aliases') or {}
        canonical=aliases.get(item_key,item_key); target=items.get(canonical)
        if not target:return jsonify(ok=False,message='Item not cached'),404
        force=str(request.args.get('refresh') or '').lower() in ('1','true','yes')

        # Fingerprint evidence, not folders/timestamps. Filing a paper does not invalidate Related;
        # adding/changing title or abstract does.
        evidence_rows=[]
        for k,r in sorted(items.items()):
            if aliases.get(k,k)!=k: continue
            pid=_paper_identity(r)
            evidence_rows.append((pid,_norm_title(str(r.get('title') or '')),hashlib.sha1(str(r.get('abstract') or '').encode('utf-8')).hexdigest()[:12]))
        library_fp=hashlib.sha1(json.dumps(evidence_rows,ensure_ascii=False,sort_keys=True).encode('utf-8')).hexdigest()[:20]
        target_pid=_paper_identity(target)
        db=_json_read(RELATED_RESULTS_FILE,{})
        saved=db.get(target_pid) if isinstance(db,dict) else None
        if saved and saved.get('library_fingerprint')==library_fp and not force:
            out=dict(saved)
            out.update({'ok':True,'cached':True,'gemini_calls':0})
            return jsonify(out)

        query=(str(target.get('title') or '')+'\n'+str(target.get('abstract') or '')[:3500]).strip()
        # Local recall is intentionally permissive; the Primary LLM is the precision stage.
        rows,stats=knowledge_retrieve(cache,query,limit=18,threshold=0)
        ded=[]; seen=set()
        for r in rows:
            k=aliases.get(r.get('key'),r.get('key'))
            if not k or k==canonical or k in seen or k not in items: continue
            seen.add(k); src=items[k]
            ded.append({'key':k,'title':src.get('title','Untitled'),'year':src.get('year',''),
                        'abstract':str(src.get('abstract') or '')[:3500],
                        'local_relevance':r.get('relevance',0),
                        'evidence':'abstract' if str(src.get('abstract') or '').strip() else 'metadata'})
            if len(ded)>=12: break
        # Defensive lexical fallback in case the embedding/local retrieval index is sparse/stale.
        if len(ded)<5:
            import re as _re, math as _math
            stop={'the','and','for','with','from','using','into','this','that','are','was','were','study','paper','method','methods','model','models'}
            def toks(x): return {w for w in _re.findall(r'[a-z][a-z0-9-]{2,}',str(x or '').lower()) if w not in stop}
            a=toks(str(target.get('title') or '')+' '+str(target.get('abstract') or ''))
            extra=[]
            for k,src in items.items():
                if aliases.get(k,k)!=k or k==canonical or k in seen: continue
                b=toks(str(src.get('title') or '')+' '+str(src.get('abstract') or '')[:2500])
                score=(len(a&b)/_math.sqrt(max(1,len(a))*max(1,len(b)))) if a and b else 0
                extra.append((score,k,src))
            for score,k,src in sorted(extra,reverse=True)[:max(0,12-len(ded))]:
                if score<=0: continue
                seen.add(k); ded.append({'key':k,'title':src.get('title','Untitled'),'year':src.get('year',''),
                    'abstract':str(src.get('abstract') or '')[:3500],'local_relevance':round(score*100,1),
                    'evidence':'abstract' if str(src.get('abstract') or '').strip() else 'metadata'})
        if not ded:
            payload={'item_key':canonical,'paper_id':target_pid,'title':target.get('title','Untitled'),'related':[],
                     'library_fingerprint':library_fp,'screened_locally':len(items),'candidates':0,
                     'message':'No plausible local candidates were found.','saved_at':datetime.now().isoformat(timespec='seconds')}
            db[target_pid]=payload; _json_write(RELATED_RESULTS_FILE,db)
            return jsonify(ok=True,cached=False,gemini_calls=0,**payload)

        # The Primary LLM is an optional relation-labeling enhancement, never a hard dependency.
        # The local candidate list is already useful, so a slow/failed model must degrade gracefully.
        def local_fallback(reason='Local similarity ranking'):
            out=[]
            for r in sorted(ded,key=lambda z:float(z.get('local_relevance') or 0),reverse=True)[:8]:
                score=max(1,min(100,int(round(float(r.get('local_relevance') or 0)))))
                out.append({'key':r['key'],'title':r['title'],'year':r['year'],'score':score,
                            'relationship':'local-similarity','reason':reason,'evidence':r['evidence']})
            return out

        agent=load_agent()
        prompt="""Rank scientifically related papers from ONE personal literature library. Use ONLY the supplied title/abstract/metadata. Do not infer full-text facts.\n\nRelationship must be one of: same-problem, same-method, extension, validation, contrast, supporting-evidence, background, technical-comparison.\nReturn JSON only: an array of at most 8 objects {key, score, relationship, reason}. score is 0-100. reason is one concise sentence explaining the scientific relationship. Exclude weak/unrelated candidates rather than filling the quota.\n\nTARGET=%s\n\nCANDIDATES=%s""" % (
            json.dumps({'title':target.get('title'),'year':target.get('year'),'abstract':str(target.get('abstract') or '')[:4500]},ensure_ascii=False),
            json.dumps([{k:v for k,v in r.items() if k in ('key','title','year','abstract','evidence')} for r in ded],ensure_ascii=False))

        box={}
        def gemini_worker():
            try:
                inter=agent.gemini.interactions.create(model=agent.MODEL,input=prompt)
                box['raw']=(inter.output_text or '').strip()
            except Exception as e:
                box['error']=f'{type(e).__name__}: {e}'
        th=threading.Thread(target=gemini_worker,daemon=True); th.start()
        timeout_s=max(8,min(30,int((read_settings().get('related') or {}).get('gemini_timeout_seconds',20))))
        th.join(timeout_s)

        by={r['key']:r for r in ded}; result=[]; gemini_calls=1; degraded=False; degradation_reason=''
        if th.is_alive():
            degraded=True; degradation_reason=f'Primary LLM timed out after {timeout_s}s; showing local similarity results.'
            result=local_fallback('Local similarity; LLM relation labeling timed out')
        elif box.get('error'):
            degraded=True; degradation_reason='LLM relation labeling failed; showing local similarity results.'
            result=local_fallback('Local similarity; LLM relation labeling unavailable')
        else:
            try:
                raw=str(box.get('raw') or '').replace('```json','').replace('```','').strip(); ranked=json.loads(raw)
                for x in ranked if isinstance(ranked,list) else []:
                    k=aliases.get(str(x.get('key') or ''),str(x.get('key') or ''))
                    if k not in by: continue
                    r=by[k]; score=max(0,min(100,int(float(x.get('score',0) or 0))))
                    if score<35: continue
                    result.append({'key':k,'title':r['title'],'year':r['year'],'score':score,
                                   'relationship':str(x.get('relationship') or 'background'),
                                   'reason':str(x.get('reason') or ''),'evidence':r['evidence']})
                result=sorted(result,key=lambda z:z['score'],reverse=True)[:8]
            except Exception:
                degraded=True; degradation_reason='Primary LLM returned an unusable response; showing local similarity results.'
                result=local_fallback('Local similarity; LLM response could not be parsed')

        payload={'item_key':canonical,'paper_id':target_pid,'title':target.get('title','Untitled'),'related':result,
                 'library_fingerprint':library_fp,'screened_locally':len(items),'candidates':len(ded),
                 'degraded':degraded,'degradation_reason':degradation_reason,
                 'saved_at':datetime.now().isoformat(timespec='seconds')}
        db[target_pid]=payload; _json_write(RELATED_RESULTS_FILE,db)
        return jsonify(ok=True,cached=False,gemini_calls=gemini_calls,**payload)
    except Exception as exc:
        try:
            with open(BASE_DIR/'agent.log','a',encoding='utf-8') as f:f.write('\nRELATED PAPERS ERROR\n'+traceback.format_exc()+'\n')
        except Exception: pass
        return jsonify(ok=False,error=f'{type(exc).__name__}: {exc}'),500


@app.get('/')
def home():
    from flask import redirect
    return redirect('/dashboard')

@app.get('/settings-page')
def settings_page(): return Response((TEMPLATE_DIR/'settings.html').read_text(encoding='utf-8'), mimetype='text/html')

@app.get("/history")
def history_page():
    return Response((TEMPLATE_DIR/'history.html').read_text(encoding='utf-8'), mimetype='text/html')

@app.get("/dashboard")
def dashboard():
    # The backend is the single source of truth for the application version.
    # Older dashboard.html files may contain a hard-coded version (for example v1.1.0);
    # replace that display value at response time so partial upgrades cannot show a stale version.
    import re
    html=(TEMPLATE_DIR/'dashboard.html').read_text(encoding='utf-8')
    html=re.sub(r'(Literature\s+Agent\s*(?:<[^>]+>\s*)?)v\d+\.\d+\.\d+',
                lambda m: m.group(1)+'v'+SERVICE_VERSION, html, count=1, flags=re.I)
    return Response(html, mimetype='text/html', headers={'Cache-Control':'no-store, max-age=0'})

def _startup_self_check():
    required = [
        TEMPLATE_DIR / 'dashboard.html', TEMPLATE_DIR / 'settings.html', TEMPLATE_DIR / 'history.html',
        STATIC_DIR / 'app.css', STATIC_DIR / 'app.js', BASE_DIR / 'core' / 'research_card.py', BASE_DIR / 'core' / 'local_llm.py'
    ]
    missing = [str(x.relative_to(BASE_DIR)) for x in required if not x.is_file()]
    if missing:
        raise RuntimeError('Incomplete Literature Agent installation; missing: ' + ', '.join(missing))
    required_routes={'/llm/settings','/llm/test','/local-llm/settings','/local-llm/test','/local-llm/status','/watch/compile'}
    actual={r.rule for r in app.url_map.iter_rules()}
    missing_routes=sorted(required_routes-actual)
    if missing_routes:
        raise RuntimeError('Local LLM routes missing: '+', '.join(missing_routes))
    # Guard against frontend regression: Watch Agent is required because Local LLM watch_compile
    # has no useful UI without these controls.
    dashboard_text=(TEMPLATE_DIR / 'dashboard.html').read_text(encoding='utf-8')
    required_ui=('id="watchAgentPanel"','id="watchText"','id="watchPreview"','id="applyWatchBtn"','id="candidates"')
    missing_ui=[x for x in required_ui if x not in dashboard_text]
    if missing_ui:
        raise RuntimeError('Watch Agent UI incomplete; missing markers: '+', '.join(missing_ui))
    print('Frontend self-check: OK (templates + CSS + JS + Research Card + Watch Agent UI + Local LLM routes)')

def main():
    _startup_self_check()
    print(f"Literature Agent V{SERVICE_VERSION}: http://{HOST}:{PORT}/dashboard")
    app.run(host=HOST, port=PORT, debug=False, threaded=True, use_reloader=False)

if __name__ == "__main__":
    main()
