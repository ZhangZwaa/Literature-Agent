from pathlib import Path

ROLES=("research","methods","review")
DEFAULTS={
"general":"Focus on decision-relevant scientific content. Be concise, evidence-grounded, and explicit about uncertainty.",
"research":"Research papers: focus on the scientific question, study design, population/data, empirical findings, quantitative results, validity, limitations, and practical/scientific implications. Distinguish findings from tools used to obtain them.",
"methods":"Methods papers: focus on methodological innovation, limitations of prior methods, technical mechanism, assumptions, training/evaluation design, benchmarks, improvements, computational requirements, reproducibility, and best-use situations.",
"review":"Review papers: focus on scope, literature organization, major themes and consensus, disagreements, evidence gaps, methodological trends, and authors' synthesis. For systematic reviews/meta-analyses preserve search/selection criteria and pooled quantitative evidence."
}
def user_dir(base): return Path(base)/'user'/'prompts'
def default_dir(base): return Path(base)/'config'/'prompts'
def ensure(base):
    ud=user_dir(base); dd=default_dir(base); ud.mkdir(parents=True,exist_ok=True); dd.mkdir(parents=True,exist_ok=True)
    for k,v in DEFAULTS.items():
        p=dd/(k+'.default.txt')
        if not p.exists(): p.write_text(v+'\n',encoding='utf-8')
    return ud,dd
def read_all(base):
    ud,dd=ensure(base); out={}
    for k,v in DEFAULTS.items():
        up=ud/(k+'.txt'); dp=dd/(k+'.default.txt')
        try: out[k]=(up if up.exists() else dp).read_text(encoding='utf-8').strip()
        except Exception: out[k]=v
    return out
def write_all(base,data):
    ud,_=ensure(base)
    for k in DEFAULTS:
        if k in data: (ud/(k+'.txt')).write_text(str(data[k]).strip()+'\n',encoding='utf-8')
    return read_all(base)
def restore(base):
    ud,dd=ensure(base)
    for k in DEFAULTS: shutil_copy(dd/(k+'.default.txt'),ud/(k+'.txt'))
    return read_all(base)
def shutil_copy(a,b): b.write_text(a.read_text(encoding='utf-8'),encoding='utf-8')
def combined(base):
    p=read_all(base)
    return "\n\n".join(["GENERAL ANALYSIS INSTRUCTION:\n"+p['general'],"RESEARCH ROLE:\n"+p['research'],"METHODS ROLE:\n"+p['methods'],"REVIEW ROLE:\n"+p['review']])
