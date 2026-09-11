import json, time, urllib.parse, urllib.request, xml.etree.ElementTree as ET
from datetime import datetime, timedelta

def http_text(url, headers=None, timeout=25):
    h={'User-Agent':'Literature-Agent/0.11 (personal research tool)'}; h.update(headers or {})
    req=urllib.request.Request(url,headers=h)
    with urllib.request.urlopen(req,timeout=timeout) as r:return r.read().decode('utf-8','replace')

def parse_iso(s):
    try:return datetime.fromisoformat(str(s))
    except Exception:return None

def effective_since(last_checked,days=0):
    dt=parse_iso(last_checked) if last_checked else None
    if days:
        floor=datetime.now()-timedelta(days=int(days))
        if not dt or floor>dt:dt=floor
    return dt
