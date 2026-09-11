import html
import re
import threading

_card_cache = {}
_card_cache_lock = threading.Lock()

def clear_card_cache(item_key=None):
    with _card_cache_lock:
        if item_key is None:
            _card_cache.clear()
        else:
            _card_cache.pop(item_key, None)

def strip_html(text):
    text = re.sub(r"<br\s*/?>", "\n", text or "", flags=re.I)
    text = re.sub(r"</p\s*>", "\n\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()

def extract_section(text, name):
    plain = strip_html(text)
    m = re.search(r"\[" + re.escape(name) + r"\]\s*(.*?)(?=\n\s*\[[^\]]+\]|\Z)", plain, re.S | re.I)
    return m.group(1).strip() if m else ""

def research_card_for_item(agent, item_key):
    with _card_cache_lock:
        if item_key in _card_cache:
            return _card_cache[item_key]
    for child in agent.get_children(item_key):
        d = child.get("data", {})
        if d.get("itemType") != "note":
            continue
        note = d.get("note", "")
        if "RESEARCH CARD" not in note.upper():
            continue
        card = {
            "paper_type": extract_section(note, "Paper Type"),
            "research_area": extract_section(note, "Research Area"),
            "methodology": extract_section(note, "Methodology"),
            "priority": extract_section(note, "Priority to Read"),
            "evidence": extract_section(note, "Evidence Strength"),
            "methodological_value": extract_section(note, "Methodological Value"),
            "novelty": extract_section(note, "Novelty"),
            "recommended_collection": extract_section(note, "Recommended Collection"),
            "citation_role": extract_section(note, "Citation Role"),
            "why_it_matters": extract_section(note, "Why It Matters"),
            "quick_meta": extract_section(note, "Quick Meta"),
            "problem": extract_section(note, "Problem"),
            "approach": extract_section(note, "Approach"),
            "results": extract_section(note, "Results"),
            "limitations": extract_section(note, "Limitations"),
            "reproducibility": extract_section(note, "Reproducibility"),
            "keywords": extract_section(note, "Keywords"),
        }
        with _card_cache_lock:
            _card_cache[item_key] = card
        return card
    with _card_cache_lock:
        _card_cache[item_key] = None
    return None
