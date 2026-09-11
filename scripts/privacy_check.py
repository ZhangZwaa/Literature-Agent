"""Conservative pre-publish check for files intended to be public."""
from pathlib import Path
import re, sys
ROOT=Path(__file__).resolve().parents[1]
PRIVATE_FILES={'.env','agent_settings.json','user/llm.json','user/local_llm.json','user/profile.json','user/prompts.json'}
PRIVATE_PREFIX=('data/','downloads/','.venv/','venv/','__pycache__/')
SECRET_PATTERNS=[
    re.compile(r'(?i)(api[_-]?key|token|secret)\s*[=:]\s*["\']?[A-Za-z0-9_\-]{20,}'),
    re.compile(r'AIza[0-9A-Za-z_-]{25,}'), re.compile(r'sk-[A-Za-z0-9_-]{20,}'), re.compile(r'gsk_[A-Za-z0-9_-]{20,}')]
issues=[]
gitignore=(ROOT/'.gitignore').read_text(encoding='utf-8',errors='ignore') if (ROOT/'.gitignore').exists() else ''
for required in ('.env','agent_settings.json','user/*.json','data/','downloads/','*.log'):
    if required not in gitignore: issues.append('missing .gitignore protection: '+required)
for p in ROOT.rglob('*'):
    if not p.is_file(): continue
    rel=p.relative_to(ROOT).as_posix()
    if rel in PRIVATE_FILES or rel.startswith(PRIVATE_PREFIX): continue
    if p.suffix.lower() in {'.pdf','.zip','.rar','.7z','.db','.sqlite','.sqlite3'}: continue
    try:text=p.read_text(encoding='utf-8',errors='ignore')
    except Exception:continue
    if rel=='.env.example':continue
    for pat in SECRET_PATTERNS:
        if pat.search(text):issues.append('possible embedded secret in '+rel);break
if issues:
    print('PRIVACY CHECK FAILED');[print(' -',x) for x in sorted(set(issues))];sys.exit(1)
print('Privacy check passed: private runtime paths are ignored and no obvious secrets were found in public-source files.')
