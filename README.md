# IBM Docs Scraper

Crawls IBM documentation and saves each page as a `.md` + `.json` pair for RAG knowledge bases.

Built for: **IBM watsonx Assistant for Z (WXA4Z)** on s390x and x86.

The scraper also cleans common docs boilerplate and adds retrieval keywords so RAG systems can match user intent faster, even when users search with aliases like `wxa4z`, `IBM Z`, `pvc`, or `knowledge base`.

---

## Setup

```bash
pip install beautifulsoup4 markdownify playwright
playwright install chromium
```

> Playwright is required — IBM docs are JavaScript-rendered and won't work with a plain HTTP client.

---

## Output Structure

```
output/
  content/                  ← markdown files (point your RAG at this folder)
    page-title.md
  metadata/                 ← JSON files (tags, links, timestamps)
    page-title.json
  .crawl_state.json         ← checkpoint for resuming interrupted crawls
```

### Metadata Format

Each `.json` file contains:

```json
{
  "url": "https://www.ibm.com/docs/en/watsonx/waz/3.2.0?topic=...",
  "title": "Install watsonx Assistant For Z (s390x)",
  "scraped_at": "2026-06-03T04:01:27+00:00",
  "source": "www.ibm.com",
  "links_to": ["https://..."],
  "content_links": ["https://..."],
  "tags": [
    "deployment=s390x",
    "product=wxa4z",
    "source=ibm-docs",
    "topic=gpu",
    "topic=install",
    "topic=storage",
    "version=3.2"
  ],
  "keywords": [
    "install",
    "s390x",
    "watsonx assistant for z",
    "persistent volume",
    "pvc"
  ],
  "search_text": "Install watsonx Assistant For Z deployment=s390x ..."
}
```

Markdown files also include frontmatter with `title`, `url`, `tags`, and `keywords` before the cleaned page body. This helps simple RAG pipelines that only index the `content/` folder and ignore separate metadata files.

Tags are broad auto-generated filters from the page URL and content. Possible values:

| Tag | Values |
|---|---|
| `deployment` | `s390x`, `x86`, `hybrid` |
| `topic` | `gpu`, `storage`, `networking`, `ifl`, `install`, `upgrade`, `troubleshooting`, `agents`, `auth`, `operators`, `ingestion`, `release-notes`, `faqs` |
| `version` | `3.2`, etc. (pulled from URL) |
| `product` | `wxa4z` |
| `source` | `ibm-docs` |

Keywords are narrower retrieval hints generated from page headings, repeated domain terms, known aliases, tags, and version strings. Use them for search boosting, hybrid retrieval, metadata filters, or prompt context.

---

## Usage

```bash
python scraper.py [options]
```

### Options

| Flag | Default | Description |
|---|---|---|
| `--seed URL` | IBM Software Hub 5.3 | Starting page. Pass multiple times for multiple seeds |
| `--output-dir` | `output/` | Where to save files |
| `--delay` | `1.0` | Seconds between requests. Increase if IBM rate-limits you |
| `--limit` | none | Stop after N pages. Use for test runs |
| `--resume` | off | Resume an interrupted crawl from checkpoint |
| `--checkpoint` | `.crawl_state.json` | Checkpoint filename inside output-dir |
| `--rewrite-links` | off | Rewrite IBM URLs in `.md` files to local relative links. No pages fetched |

### Typical Workflow

```bash
# 1. Test run — 10 pages to verify everything looks right
python scraper.py --seed "https://www.ibm.com/docs/en/watsonx/waz/3.2.0?topic=overview-watsonx-assistant-z" --limit 10

# 2. Full crawl
python scraper.py --seed "https://www.ibm.com/docs/en/watsonx/waz/3.2.0?topic=overview-watsonx-assistant-z"

# 3. Resume if interrupted
python scraper.py --seed "..." --resume

# 4. Multiple seed pages
python scraper.py --seed "https://ibm.com/docs/en/PAGE-1" --seed "https://ibm.com/docs/en/PAGE-2"

# 5. Rewrite internal links to local files
python scraper.py --rewrite-links
```

> Always quote URLs — zsh treats `?` as a glob character.

### If IBM Blocks You

```bash
python scraper.py --seed "..." --delay 3
```

---

## Notes

- The scraper stays within the same version path as your seed URL and won't wander to other IBM products
- Announcement pages (`?announcement=`) and placeholder pages (`dummy`) are automatically skipped
- Failed pages (timeout, 404) are logged and skipped — the crawl continues
- The same URL is never scraped twice in a single run
