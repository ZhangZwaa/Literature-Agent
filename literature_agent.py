import os
import re
import html
import tempfile
import threading
from datetime import datetime
from pathlib import Path

import pymupdf
from dotenv import load_dotenv
from pyzotero import zotero
from core.llm_provider import PrimaryLLM
from core.prompt_service import combined as role_prompt_text


# ============================================================
# CONFIG
# ============================================================

ROOT_COLLECTIONS = [
    "00_Inbox",
    "01_Research",
    "02_Methods",
    "03_Review",
    "04_Projects",
]

FULL_ANALYSIS_TIMEOUT_SECONDS = int(os.getenv("LLM_FULL_ANALYSIS_TIMEOUT_SECONDS", os.getenv("GEMINI_FULL_ANALYSIS_TIMEOUT_SECONDS", "90")))
FULL_ANALYSIS_RETRIES = 1

AI_NOTE_MARKER = "AI Literature Summary"
AI_TAG = "ai-summarized"

WRITE_TO_ZOTERO = True


# ============================================================
# ENVIRONMENT
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

ZOTERO_USER_ID = os.getenv("ZOTERO_USER_ID")
ZOTERO_API_KEY = os.getenv("ZOTERO_API_KEY")

if not ZOTERO_USER_ID:
    raise RuntimeError("Missing ZOTERO_USER_ID in .env")

if not ZOTERO_API_KEY:
    raise RuntimeError("Missing ZOTERO_API_KEY in .env")



# ============================================================
# CLIENTS
# ============================================================

zot = zotero.Zotero(
    ZOTERO_USER_ID,
    "user",
    ZOTERO_API_KEY
)

primary_llm = PrimaryLLM(BASE_DIR / "user" / "llm.json", BASE_DIR)
MODEL = primary_llm.model
# Backward-compatible name for existing server modules; this is no longer Gemini-specific.
gemini = primary_llm


# ============================================================
# COLLECTION FUNCTIONS
# ============================================================

def get_all_collections():
    return zot.collections()


def build_collection_maps(collections):

    by_key = {}
    children_map = {}

    for collection in collections:

        data = collection["data"]

        key = data["key"]
        parent = data.get("parentCollection", False)

        by_key[key] = collection

        if parent:
            children_map.setdefault(parent, []).append(key)

    return by_key, children_map


def _read_zotero_role_settings():
    try:
        import json
        path=BASE_DIR / "agent_settings.json"
        if path.exists():
            data=json.loads(path.read_text(encoding="utf-8"))
            return ((data.get("zotero_integration") or {}).get("roles") or {})
    except Exception:
        pass
    return {}


def find_root_collection_keys(collections):
    """Resolve configured Zotero role keys; names are display-only and may be renamed."""
    tops=[c for c in collections if not c.get("data",{}).get("parentCollection")]
    by_key={c.get("data",{}).get("key"):c for c in tops}
    saved=_read_zotero_role_settings(); roles=[]
    prefix_by_role={"inbox":"00","research":"01","methods":"02","review":"03","projects":"04"}
    for role in ("inbox","research","methods","review","projects"):
        key=str(saved.get(role) or "").strip()
        if key and key in by_key:
            roles.append(key); continue
        prefix=prefix_by_role[role]
        matches=[c for c in tops if str(c.get("data",{}).get("name") or "").startswith(prefix)]
        if len(matches)==1: roles.append(matches[0]["data"]["key"])
    return list(dict.fromkeys(roles))

def collect_descendant_keys(
    root_key,
    children_map
):
    """
    Return root collection plus every descendant collection.
    """

    result = []
    stack = [root_key]

    while stack:

        current = stack.pop()

        if current in result:
            continue

        result.append(current)

        children = children_map.get(
            current,
            []
        )

        stack.extend(children)

    return result


def get_target_collection_keys():

    collections = get_all_collections()

    _, children_map = build_collection_maps(
        collections
    )

    root_keys = find_root_collection_keys(
        collections
    )

    target_keys = set()

    for root_key in root_keys:

        descendants = collect_descendant_keys(
            root_key,
            children_map
        )

        target_keys.update(descendants)

    return target_keys


# ============================================================
# ITEM COLLECTION / DEDUPLICATION
# ============================================================

def collect_unique_items(collection_keys):
    """
    Collect papers from every target collection.

    Same Zotero item may appear in Research + Methods + Project.
    Item Key is therefore used as the unique identifier.
    """

    unique_items = {}

    print()
    print("Scanning collections...")

    for collection_key in collection_keys:

        try:

            items = zot.collection_items_top(
                collection_key
            )

        except Exception as error:

            print(
                f"WARNING: Could not read collection "
                f"{collection_key}: {error}"
            )

            continue

        for item in items:

            data = item.get(
                "data",
                {}
            )

            item_type = data.get(
                "itemType",
                ""
            )

            if item_type in [
                "attachment",
                "note"
            ]:
                continue

            item_key = data.get(
                "key"
            )

            if not item_key:
                continue

            unique_items[item_key] = item

    return list(
        unique_items.values()
    )


# ============================================================
# ZOTERO ITEM FUNCTIONS
# ============================================================

def get_children(item_key):

    return zot.children(
        item_key
    )


def has_ai_summary_note(item_key):

    for child in get_children(
        item_key
    ):

        data = child.get(
            "data",
            {}
        )

        if data.get(
            "itemType"
        ) != "note":
            continue

        note = data.get(
            "note",
            ""
        )

        # Support both legacy agent summaries and the current Research Card format.
        # Current cards are headed "RESEARCH CARD" and may not contain AI_NOTE_MARKER.
        if (
            AI_NOTE_MARKER.lower() in note.lower()
            or "RESEARCH CARD" in note.upper()
        ):
            return True

    return False


def has_ai_tag(item):

    tags = item["data"].get(
        "tags",
        []
    )

    for tag_object in tags:

        tag = tag_object.get(
            "tag",
            ""
        )

        if tag.lower() == AI_TAG.lower():
            return True

    return False


def is_already_summarized(item):
    """
    Note is authoritative.

    Tag is also accepted so routine scans are fast.
    """

    if has_ai_tag(item):
        return True

    item_key = item["data"]["key"]

    if has_ai_summary_note(
        item_key
    ):
        return True

    return False


def find_pdf_attachment(item_key):

    for child in get_children(
        item_key
    ):

        data = child.get(
            "data",
            {}
        )

        if data.get(
            "itemType"
        ) != "attachment":
            continue

        content_type = data.get(
            "contentType",
            ""
        )

        filename = data.get(
            "filename",
            ""
        )

        title = data.get(
            "title",
            ""
        )

        if (
            content_type == "application/pdf"
            or filename.lower().endswith(".pdf")
            or title.lower().endswith(".pdf")
        ):
            return child

    return None


def download_pdf_temporarily(
    attachment_key
):

    pdf_bytes = zot.file(
        attachment_key
    )

    temp = tempfile.NamedTemporaryFile(
        suffix=".pdf",
        delete=False
    )

    temp.write(
        pdf_bytes
    )

    temp.close()

    return temp.name


# ============================================================
# PDF EXTRACTION
# ============================================================

def extract_pdf_text(pdf_path):

    document = pymupdf.open(
        pdf_path
    )

    pages = []

    for page_number, page in enumerate(
        document,
        start=1
    ):

        text = page.get_text(
            "text"
        )

        if not text.strip():
            continue

        pages.append(
            f"\n\n"
            f"===== PAGE {page_number} ====="
            f"\n\n{text}"
        )

    document.close()

    return "".join(
        pages
    )


# ============================================================
# PROMPT
# ============================================================

def get_output_language():
    settings_path = BASE_DIR / "agent_settings.json"
    default = "english"
    try:
        if settings_path.exists():
            import json
            data = json.loads(settings_path.read_text(encoding="utf-8"))
            value = str(data.get("output_language", default)).lower()
            if value in {"english", "chinese", "bilingual"}:
                return value
    except Exception:
        pass
    return default


def get_full_card_instruction():
    path = BASE_DIR / "user" / "prompts.json"
    try:
        if path.exists():
            import json
            data=json.loads(path.read_text(encoding="utf-8"))
            return str(data.get("full_card") or "").strip()
    except Exception:
        pass
    return ""

def get_filing_roots():
    """Current user-facing names for Research/Methods/Review Zotero roles."""
    fallback={"research":"01_Research","methods":"02_Methods","review":"03_Review"}
    try:
        collections=get_all_collections(); tops={c.get("data",{}).get("key"):c for c in collections if not c.get("data",{}).get("parentCollection")}
        saved=_read_zotero_role_settings(); cfg=json.loads((BASE_DIR / "agent_settings.json").read_text(encoding="utf-8")) if (BASE_DIR / "agent_settings.json").exists() else {}; zi=cfg.get("zotero_integration") or {}; defaults=zi.get("role_defaults") or {}; sets=zi.get("role_sets") or {}; out=[]
        prefix={"research":"01","methods":"02","review":"03"}
        for role in ("research","methods","review"):
            key=str(defaults.get(role) or saved.get(role) or "").strip(); c=tops.get(key)
            if not c:
                matches=[x for x in tops.values() if str(x.get("data",{}).get("name") or "").startswith(prefix[role])]
                c=matches[0] if len(matches)==1 else None
            out.append(str(c.get("data",{}).get("name") or fallback[role]) if c else fallback[role])
        return out
    except Exception:
        return [fallback[r] for r in ("research","methods","review")]

def build_prompt(title, doi, paper_text):
    language = get_output_language()
    filing_roots = get_filing_roots()
    filing_roots_text = "\n".join(f"- {name}" for name in filing_roots)
    language_rule = {
        "english": "Write the entire output in English only.",
        "chinese": "Write the entire output in Chinese, preserving standard English scientific terms where useful.",
        "bilingual": "For each substantive section, write concise English first, then concise Chinese. Do not add information merely to make both versions longer."
    }[language]

    return f"""
You are a rigorous scientific literature analyst. Analyze ONE paper using ONLY the supplied paper text.

OUTPUT LANGUAGE
{language_rule}

CUSTOM USER INSTRUCTION
{get_full_card_instruction() or "Use the standard Research Card rules below."}

USER-EDITABLE ROLE/TAXONOMY INSTRUCTIONS
{role_prompt_text(BASE_DIR)}
First classify the paper as Research, Methods, or Review from the supplied evidence, then emphasize the corresponding role instruction. These instructions cannot override the evidence-only accuracy rules below.

CORE GOAL
Create a compact research card that lets a researcher understand the paper at a useful rough-reading level without reading the full paper. Spend tokens on the paper's reasoning and evidence, not on repetitive labels.

ACCURACY RULES
1. Never invent information or fill gaps with outside knowledge.
2. Preserve decision-relevant numerical results, sample/data scale, comparisons, effect sizes, accuracy, correlations, P values, or other key metrics when they matter.
3. Distinguish participants/patients/subjects from images, trials, cells, samples, datasets, and observations.
4. Prioritize limitations explicitly stated by the authors. Any additional inference must be labeled "AI assessment".
5. Do not repeat the same fact across sections unless essential for comprehension.
6. Do not use markdown tables.
7. Do not write anything after "Why useful to me:".

PAPER METADATA
Title: {title}
DOI: {doi if doi else "Not stated"}

FULL PAPER TEXT
{paper_text}

REQUIRED OUTPUT

RESEARCH CARD

[Quick Meta]
Compress into at most THREE short lines total. Do NOT write labels such as "Line 1", "Line 2", or "Line 3".
First line: Paper Type | Research Area | Evidence: Low/Moderate/High
Second line: 3-6 core methodology/technology terms separated by " · "
Third line: Citation: up to three roles from Background / Methods / Supporting Evidence / Contrasting Evidence / Benchmark / Discussion / Technical Reference / Clinical Evidence
Do not include rationales here.

[Recommended Collection]
Recommend at most TWO plausible filing paths. The CURRENT allowed root collection names are:
{filing_roots_text}
Use these root names EXACTLY as written above; their suffixes are user-renamable. Never invent an old root name. Never recommend a 04_Projects root. One path per line.

[Problem]
In one compact paragraph, explain the concrete scientific/technical problem, why it matters, and what limitation in prior work motivates this paper. Usually 2-4 sentences.

[Approach]
In one or two compact paragraphs, explain what the authors actually proposed or did. Include the study/data design and the technically important mechanism, model, experiment, analysis, or validation strategy. Include sample/data scale only when it materially affects interpretation. The reader should understand HOW the paper addressed the problem.

[Results]
In one or two compact paragraphs, explain what happened and whether the approach solved the stated problem. Preserve the most important quantitative results and comparisons. End with the practical/scientific implication supported by the evidence. Avoid a long bullet inventory.

[Limitations]
2-4 compact bullets containing only limitations that materially affect interpretation, generalization, reproducibility, or practical use. Prefer author-stated limitations. Label inferred limitations "AI assessment".

[Reproducibility]
One compact line: Data: ... | Code: ... | Software/Repository: ... | Supplement: ...

[Keywords]
5-8 English scientific keywords, comma-separated, regardless of output language.

[My Notes]
Why useful to me:
"""


# ============================================================
# GEMINI
# ============================================================

def summarize_paper(title, doi, paper_text, progress=None):
    """Run full-paper LLM analysis with a bounded wait and one retry."""
    prompt = build_prompt(title, doi, paper_text)
    timeout = max(15, FULL_ANALYSIS_TIMEOUT_SECONDS)
    last_error = None
    for attempt in range(FULL_ANALYSIS_RETRIES + 1):
        if progress:
            progress("gemini", f"LLM analysis · attempt {attempt + 1}/{FULL_ANALYSIS_RETRIES + 1} · timeout {timeout}s")
        box = {}
        def worker():
            try:
                interaction = gemini.interactions.create(model=MODEL, input=prompt)
                box["text"] = (interaction.output_text or "").strip()
            except Exception as exc:
                box["error"] = exc
        th = threading.Thread(target=worker, daemon=True)
        th.start()
        th.join(timeout)
        if th.is_alive():
            last_error = TimeoutError(f"LLM full-paper analysis timed out after {timeout} seconds")
            if progress and attempt < FULL_ANALYSIS_RETRIES:
                progress("gemini_retry", f"LLM timed out after {timeout}s · retrying once")
            continue
        if box.get("error") is not None:
            last_error = box["error"]
            if progress and attempt < FULL_ANALYSIS_RETRIES:
                progress("gemini_retry", f"LLM request failed · retrying once: {type(last_error).__name__}")
            continue
        if box.get("text"):
            return box["text"]
        last_error = RuntimeError("LLM returned an empty response.")
    raise RuntimeError(f"LLM analysis failed after {FULL_ANALYSIS_RETRIES + 1} attempt(s): {last_error}")


# ============================================================
# NOTE CREATION
# ============================================================

def summary_to_html(summary):

    escaped = html.escape(
        summary
    )

    blocks = re.split(
        r"\n\s*\n",
        escaped
    )

    html_blocks = []

    for block in blocks:

        block = block.strip()

        if not block:
            continue

        block = block.replace(
            "\n",
            "<br>"
        )

        html_blocks.append(
            f"<p>{block}</p>"
        )

    return "".join(
        html_blocks
    )


def write_summary_to_zotero(
    item_key,
    summary
):

    template = zot.item_template(
        "note"
    )

    template["note"] = summary_to_html(
        summary
    )

    template["parentItem"] = item_key

    return zot.create_items(
        [template]
    )


# ============================================================
# TAGGING
# ============================================================

def _is_zotero_version_conflict(exc):
    text=(type(exc).__name__+" "+str(exc)).lower()
    return ("preconditionfailed" in text or "precondition failed" in text or
            "code: 412" in text or "status 412" in text or "http 412" in text)

def _sync_status_tags_enabled():
    try:
        import json
        p=BASE_DIR / "agent_settings.json"
        if p.exists():
            cfg=json.loads(p.read_text(encoding="utf-8"))
            return bool((cfg.get("zotero_integration") or {}).get("sync_status_tags", True))
    except Exception:
        pass
    return True


def add_ai_tag(item):
    """Add the helper tag using a fresh Zotero version; the Research Card note is authoritative."""
    if not _sync_status_tags_enabled():
        return True
    item_key=(item.get("data", {}) if isinstance(item, dict) else {}).get("key") or str(item or "")
    if not item_key:
        return False
    last=None
    for attempt in range(2):
        fresh=zot.item(item_key)
        if not fresh or not fresh.get("data"):
            raise RuntimeError(f"Zotero item not found while tagging: {item_key}")
        tags=fresh["data"].get("tags", []) or []
        if any(str(t.get("tag", "")).lower()==AI_TAG.lower() for t in tags):
            return True
        fresh["data"]["tags"]=tags+[{"tag":AI_TAG}]
        try:
            zot.update_item(fresh)
            return True
        except Exception as exc:
            last=exc
            if not _is_zotero_version_conflict(exc) or attempt>=1:
                raise
    raise last


# ============================================================
# PROCESS PAPER
# ============================================================

def process_paper(item, progress=None):

    data = item["data"]

    title = data.get(
        "title",
        "Untitled"
    )

    doi = data.get(
        "DOI",
        ""
    )

    item_key = data.get(
        "key",
        ""
    )

    print()
    print("=" * 80)
    print("PAPER")
    print("=" * 80)

    print(title)

    print(
        "DOI:",
        doi if doi else "No DOI"
    )

    print(
        "Item Key:",
        item_key
    )

    # --------------------------------------------------------
    # Already processed
    # --------------------------------------------------------

    if is_already_summarized(
        item
    ):

        print(
            "SKIPPED: Already summarized."
        )

        return "already_done"

    # --------------------------------------------------------
    # PDF
    # --------------------------------------------------------

    attachment = find_pdf_attachment(
        item_key
    )

    if attachment is None:

        print(
            "SKIPPED: No PDF attachment."
        )

        return "no_pdf"

    attachment_key = attachment[
        "data"
    ]["key"]

    temp_pdf = None

    try:

        print(
            "PDF found."
        )

        print(
            "Reading PDF from Zotero..."
        )
        if progress: progress("reading_pdf", "Reading PDF from Zotero…")

        temp_pdf = download_pdf_temporarily(
            attachment_key
        )

        print(
            "Extracting text with PyMuPDF..."
        )

        paper_text = extract_pdf_text(
            temp_pdf
        )
        if progress: progress("pdf_extracted", f"PDF text extracted · {len(paper_text):,} characters")

        char_count = len(
            paper_text
        )

        print(
            f"Extracted approximately "
            f"{char_count:,} characters."
        )

        if char_count < 1000:

            print(
                "FAILED: Too little extractable text."
            )

            return "failed"

        # ----------------------------------------------------
        # Primary LLM
        # ----------------------------------------------------

        print(
            f"Analyzing with {MODEL}..."
        )

        summary = summarize_paper(
            title,
            doi,
            paper_text,
            progress=progress
        )
        if progress: progress("gemini_done", "LLM response received")

        # ----------------------------------------------------
        # Zotero write
        # ----------------------------------------------------

        if WRITE_TO_ZOTERO:

            print(
                "Writing AI Summary to Zotero..."
            )
            if progress: progress("writing_card", "Writing Research Card to Zotero…")

            write_summary_to_zotero(
                item_key,
                summary
            )

            # Only tag AFTER successful note creation.
            print(
                f"Adding tag: {AI_TAG}"
            )

            try:
                add_ai_tag(item_key)
            except Exception as tag_error:
                # The Research Card note is authoritative. A helper-tag PATCH must never
                # turn a successfully generated Card into a failed analysis.
                print(f"WARNING: Research Card saved, but helper tag could not be updated: {tag_error}")

            print(
                "DONE."
            )
            if progress: progress("done", "Research Card complete")

        else:

            print()
            print(summary)
            print()
            print(
                "PREVIEW MODE: Zotero not modified."
            )

        return "generated"

    finally:

        if (
            temp_pdf
            and os.path.exists(temp_pdf)
        ):

            try:

                os.remove(
                    temp_pdf
                )

            except Exception:

                pass


# ============================================================
# MAIN
# ============================================================

def main():

    start_time = datetime.now()

    print()
    print("=" * 80)
    print("ZOTERO LITERATURE AGENT")
    print("=" * 80)

    print(
        "Started:",
        start_time.strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    )

    print(
        "Model:",
        MODEL
    )

    print(
        "Scanning roots:"
    )

    for name in ROOT_COLLECTIONS:
        print(
            " -",
            name
        )

    # --------------------------------------------------------
    # Collections
    # --------------------------------------------------------

    collection_keys = get_target_collection_keys()

    print()
    print(
        f"Collections/subcollections found: "
        f"{len(collection_keys)}"
    )

    # --------------------------------------------------------
    # Unique papers
    # --------------------------------------------------------

    items = collect_unique_items(
        collection_keys
    )

    print(
        f"Unique Zotero items found: "
        f"{len(items)}"
    )

    # --------------------------------------------------------
    # Counters
    # --------------------------------------------------------

    checked = 0
    generated = 0
    already_done = 0
    no_pdf = 0
    failed = 0

    # --------------------------------------------------------
    # Process
    # --------------------------------------------------------

    for item in items:

        checked += 1

        try:

            status = process_paper(
                item
            )

            if status == "generated":
                generated += 1

            elif status == "already_done":
                already_done += 1

            elif status == "no_pdf":
                no_pdf += 1

            else:
                failed += 1

        except Exception as error:

            failed += 1

            print()
            print(
                "ERROR processing paper:"
            )

            print(
                type(error).__name__
            )

            print(
                error
            )

            print(
                "Continuing with next paper..."
            )

    # --------------------------------------------------------
    # Finish
    # --------------------------------------------------------

    end_time = datetime.now()

    duration = end_time - start_time

    print()
    print()
    print("=" * 80)
    print("SCAN COMPLETE")
    print("=" * 80)

    print(
        f"Unique papers checked: {checked}"
    )

    print(
        f"New summaries:         {generated}"
    )

    print(
        f"Already summarized:    {already_done}"
    )

    print(
        f"No PDF:                {no_pdf}"
    )

    print(
        f"Failed:                {failed}"
    )

    print(
        f"Duration:               {duration}"
    )

    print()


if __name__ == "__main__":
    main()