# Literature Agent

**Literature Agent** is a local-first AI research assistant built around
**Zotero**. It helps you discover, organize, read, compare, and query
scientific literature while keeping Zotero as the source of truth for
papers, PDFs, metadata, and collections.

## What it does

-   **Zotero integration** --- sync your library, manage collection
    roles, file/move papers, and keep Research Cards available as Zotero
    notes.
-   **Research Cards** --- analyze paper PDFs into compact summaries of
    the problem, approach, results, limitations, reproducibility, and
    keywords.
-   **Watch Agent** --- search PubMed, Europe PMC, and arXiv for new
    literature, screen candidates, and accept useful papers into Zotero.
-   **PDF recovery** --- search legal open-access sources including
    PMC/Europe PMC, arXiv, Unpaywall, OpenAlex, Semantic Scholar, and
    Crossref.
-   **Knowledge Agent** --- find papers or synthesize answers from your
    own library.
-   **Related Papers & Comparisons** --- discover relationships inside
    your library and compare 2--5 selected papers.
-   **Configurable LLMs** --- use Gemini, Ollama, or an
    OpenAI-compatible API. Model choice and local configuration are not
    hard-coded.
-   **Editable analysis instructions** --- customize General, Research,
    Methods, and Review prompts from Settings or local prompt files.

## Quick start

### 1. Install

``` powershell
git clone https://github.com/ZhangZwaa/Literature-Agent.git
cd Literature-Agent
pip install -r requirements.txt
```

### 2. Configure credentials

Copy `.env.example` to `.env`:

``` powershell
Copy-Item .env.example .env
```

Then add your Zotero credentials and the API key for the LLM provider
you want to use.

> `.env`, local settings, prompts, PDFs, caches, databases, logs, and
> personal research data are excluded from Git.

### 3. Start

``` powershell
python agent_server.py
```

Open:

``` text
http://127.0.0.1:8765/dashboard
```

Then visit **Settings** to configure your LLM, Zotero collection roles,
analysis instructions, PDF recovery, and automation preferences.

## Typical workflow

``` text
Watch / Zotero
      ↓
   00_Inbox
      ↓
Research Card
      ↓
Review & File
      ↓
Related / Compare / Knowledge
```

Zotero remains responsible for bibliographic metadata, PDFs,
collections, syncing, and citation workflows. Literature Agent adds the
AI and research-workflow layer on top.

## Privacy

Literature Agent is designed for local use. Before publishing changes,
run:

``` powershell
python scripts\privacy_check.py
```

Do not commit `.env` or files under private runtime directories. The
included `.gitignore` is configured to exclude common personal data and
secrets.

## Status

Current version: **v1.6.2**

This project is under active development. Back up your Zotero library
and local Agent data before testing major updates.
