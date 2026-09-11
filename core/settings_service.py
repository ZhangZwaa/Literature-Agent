from pathlib import Path
import json

BASE_DIR=None; SETTINGS_FILE=None; DEFAULT_CONFIG_FILE=None; USER_PROFILE_FILE=None; PROMPTS_FILE=None
DEFAULT_PROMPTS = {
    "watch_compile": "Convert natural-language watch instructions into a safe proposed literature-search configuration. Preserve unspecified values and return JSON only.",
    "watch_screen": "Score candidates primarily for relevance, then evidence, methodological value, novelty, reproducibility, and recency. Use only title, abstract, and metadata.",
    "light_analysis": "Classify using only title, abstract, and metadata. Recommend up to two concise Zotero collection paths. Never assume full-text details.",
    "related": "Rank scientifically related papers using supplied local metadata/notes only. Do not invent relationships.",
    "full_card": "Create a compact Research Card from PDF text: concise metadata, then enough Problem/Approach/Results or equivalent narrative to approximate a rough full-paper read."
}

def configure(base_dir, settings_file, default_config_file, user_profile_file, prompts_file):
    global BASE_DIR,SETTINGS_FILE,DEFAULT_CONFIG_FILE,USER_PROFILE_FILE,PROMPTS_FILE
    BASE_DIR=Path(base_dir); SETTINGS_FILE=Path(settings_file); DEFAULT_CONFIG_FILE=Path(default_config_file); USER_PROFILE_FILE=Path(user_profile_file); PROMPTS_FILE=Path(prompts_file)

def deep_merge(a,b):
    out=dict(a or {})
    for k,v in (b or {}).items():
        out[k]=deep_merge(out.get(k),v) if isinstance(v,dict) and isinstance(out.get(k),dict) else v
    return out

def read_settings():
    defaults={"output_language":"english"}
    try:
        if DEFAULT_CONFIG_FILE.exists(): defaults=deep_merge(defaults,json.loads(DEFAULT_CONFIG_FILE.read_text(encoding="utf-8")))
        if SETTINGS_FILE.exists(): defaults=deep_merge(defaults,json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
        if USER_PROFILE_FILE.exists(): defaults["user_profile"]=json.loads(USER_PROFILE_FILE.read_text(encoding="utf-8"))
    except Exception: pass
    if defaults.get("output_language") not in ("english","chinese","bilingual"): defaults["output_language"]="english"
    if isinstance(defaults.get('limits'),dict): defaults['limits'].pop('monthly_budget_usd',None)
    return defaults

def write_settings(data):
    current=read_settings(); current.pop("user_profile",None); merged=deep_merge(current,data or {})
    lang=str(merged.get("output_language","english")).lower()
    if lang not in ("english","chinese","bilingual"): raise ValueError("invalid output_language")
    merged["output_language"]=lang; SETTINGS_FILE.write_text(json.dumps(merged,ensure_ascii=False,indent=2),encoding="utf-8"); return merged

def read_profile():
    if USER_PROFILE_FILE.exists():
        try:return json.loads(USER_PROFILE_FILE.read_text(encoding="utf-8"))
        except Exception:pass
    example=BASE_DIR/'user'/'profile.example.json'
    return json.loads(example.read_text(encoding='utf-8')) if example.exists() else {}

def write_profile(profile):
    USER_PROFILE_FILE.parent.mkdir(parents=True,exist_ok=True); USER_PROFILE_FILE.write_text(json.dumps(profile,ensure_ascii=False,indent=2),encoding='utf-8'); return profile

def read_prompts():
    out=dict(DEFAULT_PROMPTS)
    try:
        if PROMPTS_FILE.exists():out.update(json.loads(PROMPTS_FILE.read_text(encoding='utf-8')))
    except Exception:pass
    return out

def write_prompts(data):
    PROMPTS_FILE.parent.mkdir(parents=True,exist_ok=True); out=dict(DEFAULT_PROMPTS); out.update({k:str(v) for k,v in (data or {}).items() if k in DEFAULT_PROMPTS}); PROMPTS_FILE.write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8'); return out
