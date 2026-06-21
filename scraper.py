import json
import time
import re
import argparse
import threading
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
from queue import Queue, Empty

from bs4 import BeautifulSoup
from markdownify import markdownify
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout


CHECKPOINT_FILE = ".crawl_state.json"
MAX_KEYWORDS = 30

BLOCKED_PATHS = [
    "/contact", "/privacy", "/terms", "/legal", "/accessibility",
    "/sitemap", "/support", "/community", "/feedback", "/help",
    "/search", "/login", "/logout", "/register", "/account",
    "/cart", "/shop", "/store", "/news", "/blog", "/events", "/about",
]

STOPWORDS = {
    "about", "above", "after", "again", "against", "also", "and", "any", "are",
    "because", "been", "before", "being", "below", "between", "both", "but",
    "can", "cannot", "click", "com", "copy", "could", "did", "does", "doing", "done",
    "docs", "documentation", "each", "for", "following", "from", "get", "had",
    "has", "have", "having", "here", "how", "html", "https", "ibm",
    "into", "its", "more", "must", "not", "now", "off", "once", "only", "onto",
    "other", "our", "out", "over", "page", "see", "should", "such", "than",
    "that", "the", "their", "then", "there", "these", "this", "those", "through",
    "to", "under", "use", "used", "using", "was", "were", "what", "when",
    "where", "which", "while", "who", "why", "will", "with", "within", "would",
    "you", "your",
}

KEYWORD_ALIASES = {
    "assistant": ["watsonx assistant for z", "wxa4z", "assistant for z"],
    "certificate": ["tls", "certificates", "secret"],
    "deployment": ["install", "deploy", "installation"],
    "gpu": ["nvidia", "accelerator", "inference"],
    "ingestion": ["knowledge base", "corpus", "document store", "rag"],
    "operator": ["olm", "subscription", "operand"],
    "persistent volume": ["pvc", "storage class", "odf"],
    "s390x": ["ibm z", "mainframe", "linux on ibm z", "ifl"],
    "troubleshooting": ["error", "failure", "debug", "diagnose"],
    "upgrade": ["migration", "migrate", "transition"],
}

NOISY_KEYWORDS = {
    "clipboard", "command", "command obtain", "copy clipboard", "dark mode",
    "display", "filter titles", "font-family", "found", "ibm docs",
    "ibm documentation", "important", "lang", "last updated", "offline docs",
    "position", "sans", "sans-serif", "span", "table contents", "topic",
    "truste-messagecolumn", "width", "www",
}

NOISY_KEYWORD_PATTERNS = [
    r"\bhtml\b",
    r"\bwww\b",
    r"\btruste\b",
    r"\bfont\b",
    r"\bsans\b",
    r"\bclipboard\b",
    r"\b\d+\.\d+\s+\d+\.\d+\b",
]


# ── Helpers ────────────────────────────────────────────────────────────────────

def is_blocked(url):
    parsed = urlparse(url)
    path = parsed.path.lower()
    query = parsed.query.lower()
    if any(path == seg or path.startswith(seg + "/") for seg in BLOCKED_PATHS):
        return True
    if query.startswith("announcement="):
        return True
    if "dummy" in query:
        return True
    return False


def slugify(text):
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text[:100] or "untitled"


def run_folder_name(seed_url):
    parsed = urlparse(seed_url)
    parts = [parsed.netloc.replace("www.", "").replace(".", "-")]
    parts.extend(part.replace(".", "-") for part in parsed.path.split("/") if part)

    query = parsed.query
    topic_match = re.search(r"(?:^|&)topic=([^&]+)", query)
    if topic_match:
        parts.append(topic_match.group(1))

    base = slugify("-".join(parts))
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{base}-{timestamp}"


def resolve_output_dir(args):
    base_output_dir = Path(args.output_dir)
    if args.resume or args.rewrite_links:
        return base_output_dir
    return base_output_dir / run_folder_name(args.seed[0])


def resolve_url(base, href):
    href = href.split("#")[0].strip()
    if not href:
        return None
    full = urljoin(base, href)
    parsed = urlparse(full)
    if parsed.scheme not in ("http", "https"):
        return None
    return full.rstrip("/")


def in_scope(url, roots):
    parsed = urlparse(url)
    for root in roots:
        rp = urlparse(root)
        if parsed.netloc == rp.netloc and parsed.path.startswith(rp.path):
            return True
    return False


def extract_links(soup, page_url, roots):
    links = set()
    for tag in soup.find_all("a", href=True):
        resolved = resolve_url(page_url, tag["href"])
        if resolved and in_scope(resolved, roots) and not is_blocked(resolved):
            links.add(resolved)
    return links


def clean_markdown(markdown):
    boilerplate_patterns = [
        r"(?im)^Documentation$",
        r"(?im)^My IBM$",
        r"(?im)^Log in$",
        r"(?im)^Dark mode$",
        r"(?im)^Close table of contents$",
        r"(?im)^Change version$",
        r"(?im)^Select$",
        r"(?im)^Show full table of contents$",
        r"(?im)^Filter on titles$",
        r"(?im)^Download PDF$",
        r"(?im)^Offline docs$",
        r"(?im)^Copy to clipboard$",
        r"(?im)^Last Updated:.*$",
        r"(?im)^Was this (topic|page) helpful\?.*$",
        r"(?im)^Rate this (topic|page).*$",
        r"(?im)^Submit feedback.*$",
        r"(?im)^Feedback.*$",
        r"(?im)^On this page.*$",
        r"(?im)^Related information.*$",
        r"(?im)^Parent topic:.*$",
        r"(?im)^Previous topic:.*$",
        r"(?im)^Next topic:.*$",
        r"(?im)^© Copyright IBM Corporation.*[\s\S]*$",
        r"(?im)^Copyright .*$",
        r"(?im)^Contact IBM$",
        r"(?im)^Privacy$",
        r"(?im)^Terms of use$",
        r"(?im)^Accessibility$",
        r"(?im)^\d+\.\d+(?:\.\d+)?\d+\.\d+(?:\.\d+)?\d+\.\d+(?:\.\d+)?\d+\.\d+(?:\.\d+)?$",
    ]
    text = markdown
    for pattern in boilerplate_patterns:
        text = re.sub(pattern, "", text)
    text = re.sub(r"\[!\[[^\]]*\]\([^)]+\)\]\([^)]+\)", "", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_headings(markdown):
    headings = []
    for line in markdown.splitlines():
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if not match:
            continue
        heading = re.sub(r"\s+", " ", match.group(2)).strip(" #")
        if heading:
            headings.append(heading)
    return headings


def tokenize_for_keywords(text):
    return re.findall(r"\b\d+(?:\.\d+)+\b|\b[a-z][a-z0-9+#-]{2,}\b", text.lower())


def contains_keyword(text, keyword):
    pattern = re.escape(keyword.lower())
    pattern = pattern.replace(r"\ ", r"\s+")
    return re.search(rf"(?<![a-z0-9]){pattern}(?![a-z0-9])", text.lower()) is not None


def add_keyword(score_map, keyword, score):
    keyword = re.sub(r"\s+", " ", keyword.lower()).strip(" -_.,:;()[]{}")
    if not keyword or keyword in STOPWORDS:
        return
    if len(keyword) < 3:
        return
    if keyword in NOISY_KEYWORDS:
        return
    if any(re.search(pattern, keyword) for pattern in NOISY_KEYWORD_PATTERNS):
        return
    score_map[keyword] = score_map.get(keyword, 0) + score


def text_for_keywords(markdown):
    text = re.sub(r"```[\s\S]*?```", " ", markdown)
    text = re.sub(r"`[^`]+`", " ", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"/[\w./?=&%-]+", " ", text)
    return text


def extract_candidate_phrases(text):
    phrase_scores = {}
    words = tokenize_for_keywords(text)
    window = []
    for word in words:
        if word in STOPWORDS:
            if len(window) >= 2:
                for size in (2, 3):
                    for i in range(0, max(0, len(window) - size + 1)):
                        add_keyword(phrase_scores, " ".join(window[i:i + size]), 2)
            window = []
            continue
        window.append(word)
        if len(window) > 5:
            window.pop(0)
    if len(window) >= 2:
        for size in (2, 3):
            for i in range(0, max(0, len(window) - size + 1)):
                add_keyword(phrase_scores, " ".join(window[i:i + size]), 2)
    return phrase_scores


def extract_keywords(url, title, markdown, tags):
    text = f"{title}\n{text_for_keywords(markdown)}"
    lower_text = text.lower()
    scores = {}

    for token in tokenize_for_keywords(text):
        if token not in STOPWORDS:
            add_keyword(scores, token, 1)

    for phrase, score in extract_candidate_phrases(text).items():
        add_keyword(scores, phrase, score)

    for heading in extract_headings(markdown):
        add_keyword(scores, heading, 8)
        for token in tokenize_for_keywords(heading):
            add_keyword(scores, token, 4)

    for tag in tags:
        if "=" in tag:
            _, value = tag.split("=", 1)
            add_keyword(scores, value.replace("-", " "), 10)

    for canonical, aliases in KEYWORD_ALIASES.items():
        if contains_keyword(lower_text, canonical) or any(contains_keyword(lower_text, alias) for alias in aliases):
            add_keyword(scores, canonical, 12)
            for alias in aliases:
                if contains_keyword(lower_text, alias):
                    add_keyword(scores, alias, 8)

    version_match = re.search(r'/(\d+\.\d+(?:\.\d+)?)(?:[/?]|$)', url)
    if version_match:
        add_keyword(scores, version_match.group(1), 10)

    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return [keyword for keyword, _ in ranked[:MAX_KEYWORDS]]


def render_frontmatter(url, title, tags, keywords):
    def yaml_list(values):
        return "\n".join(f"  - {json.dumps(value)}" for value in values)

    return "\n".join([
        "---",
        f"title: {json.dumps(title)}",
        f"url: {json.dumps(url)}",
        "tags:",
        yaml_list(tags),
        "keywords:",
        yaml_list(keywords),
        "---",
        "",
    ])


# ── Scraping ───────────────────────────────────────────────────────────────────

def scrape_page(url, page):
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2000)
    html = page.content()
    soup = BeautifulSoup(html, "html.parser")

    content = (
        soup.find("main")
        or soup.find("article")
        or soup.find(id="content")
        or soup.find(class_="content")
        or soup.body
    )

    title = ""
    if soup.title:
        title = soup.title.get_text(strip=True)
    elif content:
        h = content.find(["h1", "h2"])
        if h:
            title = h.get_text(strip=True)

    markdown = markdownify(str(content), heading_style="ATX", strip=["script", "style"])
    markdown = clean_markdown(markdown)

    # Extract links from article body only — exclude nav/sidebar elements
    article = soup.find("article") or content
    nav_hrefs = {
        a["href"] for nav in soup.find_all("nav")
        for a in nav.find_all("a", href=True)
    }
    content_links = set()
    if article:
        for a in article.find_all("a", href=True):
            if a["href"] not in nav_hrefs:
                resolved = resolve_url(url, a["href"])
                if resolved:
                    content_links.add(resolved)

    return title, markdown, soup, content_links


# ── Auto-tagging ───────────────────────────────────────────────────────────────

def auto_tag(url, title, markdown):
    tags = []
    text = (url + " " + title + " " + markdown).lower()

    tags.append("product=wxa4z")

    version_match = re.search(r'/(\d+\.\d+)(?:\.\d+)?(?:[/?]|$)', url)
    if version_match:
        tags.append(f"version={version_match.group(1)}")

    has_s390 = bool(re.search(r's390x?|mainframe|lpar|ifl|zlinux|linux on ibm z', text))
    has_x86  = bool(re.search(r'\bx86\b|amd64|intel', text))
    if has_s390 and has_x86:
        tags.append("deployment=hybrid")
    elif has_s390:
        tags.append("deployment=s390x")
    elif has_x86:
        tags.append("deployment=x86")

    topic_rules = {
        "topic=gpu":             r'\bgpu\b|nvidia|time.?slic|inferenc',
        "topic=storage":         r'\bstorage\b|odf\b|nfs\b|ceph|pvc\b|persistent.?volume|object.?gateway',
        "topic=networking":      r'\bingress\b|egress|network.?polic|route\b|load.?balanc',
        "topic=ifl":             r'\bifl\b',
        "topic=install":         r'install|deploy|setup|prerequisite|prepare',
        "topic=upgrade":         r'upgrade|migrat|transition',
        "topic=troubleshooting": r'troubleshoot|error|fail|issue|debug|diagnos',
        "topic=agents":          r'\bagent\b|mcp.?server|tool.?call',
        "topic=auth":            r'passticket|racf|token.?exchange|secret|credential|tls|certif',
        "topic=operators":       r'\boperator\b|olm\b|subscription\b',
        "topic=ingestion":       r'ingest|knowledge.?base|corpus|document.?store',
        "topic=release-notes":   r'release.?note|what.?s.?new|change.?log',
        "topic=faqs":            r'\bfaq\b|frequently.?asked',
    }
    for tag, pattern in topic_rules.items():
        if re.search(pattern, text):
            tags.append(tag)

    tags.append("source=ibm-docs")
    return sorted(set(tags))


# ── Save ───────────────────────────────────────────────────────────────────────

def content_hash(markdown):
    normalized = re.sub(r"\s+", " ", markdown).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def find_existing_page(json_dir, md_dir, url, page_hash):
    for json_path in json_dir.glob("*.json"):
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue

        if data.get("url") == url or data.get("content_hash") == page_hash:
            return md_dir / f"{json_path.stem}.md"

    return None


def save_page(url, title, markdown, links, content_links, output_dir, roots):
    md_dir = output_dir / "md"
    json_dir = output_dir / "jsons"
    md_dir.mkdir(parents=True, exist_ok=True)
    json_dir.mkdir(parents=True, exist_ok=True)

    page_hash = content_hash(markdown)
    existing_path = find_existing_page(json_dir, md_dir, url, page_hash)
    if existing_path:
        return existing_path, False

    slug = slugify(title) if title else slugify(urlparse(url).path.replace("/", "-"))
    md_path = md_dir / f"{slug}.md"
    json_path = json_dir / f"{slug}.json"

    counter = 1
    while md_path.exists():
        existing = json_path.read_text(encoding="utf-8") if json_path.exists() else "{}"
        if json.loads(existing).get("url") == url:
            break
        md_path = md_dir / f"{slug}-{counter}.md"
        json_path = json_dir / f"{slug}-{counter}.json"
        counter += 1

    tags = auto_tag(url, title, markdown)
    keywords = extract_keywords(url, title, markdown, tags)
    md_path.write_text(
        render_frontmatter(url, title, tags, keywords) + markdown,
        encoding="utf-8",
    )
    json_path.write_text(
        json.dumps({
            "url": url,
            "title": title,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "source": urlparse(roots[0]).netloc,
            "links_to": sorted(links),
            "content_links": sorted(content_links),
            "content_hash": page_hash,
            "tags": tags,
            "keywords": keywords,
            "search_text": " ".join([title, *tags, *keywords]).strip(),
        }, indent=2),
        encoding="utf-8",
    )
    return md_path, True


# ── Checkpoint ─────────────────────────────────────────────────────────────────

def load_checkpoint(path):
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        print(f"Resuming from checkpoint: {len(data['visited'])} visited, {len(data['queue'])} queued.")
        return set(data["visited"]), data["queue"]
    return set(), []


def save_checkpoint(path, visited, queue):
    path.write_text(
        json.dumps({"visited": list(visited), "queue": list(queue)}, indent=2),
        encoding="utf-8",
    )


# ── Link rewriting ─────────────────────────────────────────────────────────────

def rewrite_links(output_dir):
    metadata_dir = output_dir / "jsons"
    content_dir = output_dir / "md"
    url_to_slug = {}
    for jf in metadata_dir.glob("*.json"):
        data = json.loads(jf.read_text(encoding="utf-8"))
        url_to_slug[data["url"]] = jf.stem

    count = 0
    for md_path in content_dir.glob("*.md"):
        text = md_path.read_text(encoding="utf-8")
        original = text
        for url, slug in url_to_slug.items():
            text = text.replace(url, f"./{slug}.md")
        if text != original:
            md_path.write_text(text, encoding="utf-8")
            count += 1

    print(f"Rewrote links in {count} files.")


# ── Crawler ────────────────────────────────────────────────────────────────────

def crawl(args):
    roots = args.seed
    output_dir = resolve_output_dir(args)
    checkpoint_path = output_dir / args.checkpoint

    visited, queued = load_checkpoint(checkpoint_path) if args.resume else (set(), [])

    queue = Queue()
    for url in (queued if queued else roots):
        queue.put(url)

    total = [0]
    done = threading.Event()

    print(f"Crawling {roots}")
    print(f"Delay: {args.delay}s | Output: {output_dir}\n")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        ).new_page()

        while not done.is_set():
            try:
                url = queue.get(timeout=2)
            except Empty:
                break

            if url in visited:
                queue.task_done()
                continue
            visited.add(url)

            try:
                total[0] += 1
                print(f"[{total[0]}] {url}")

                title, markdown, soup, content_links = scrape_page(url, page)
                new_links = extract_links(soup, url, roots) - visited
                for link in new_links:
                    queue.put(link)

                path, saved = save_page(url, title, markdown, new_links, content_links, output_dir, roots)
                if saved:
                    print(f"      → {path}")
                else:
                    print(f"      duplicate — already saved as {path}")

                save_checkpoint(checkpoint_path, visited, list(queue.queue))

                if args.limit and total[0] >= args.limit:
                    print(f"\nLimit of {args.limit} pages reached.")
                    done.set()
                    break

                time.sleep(args.delay)

            except PlaywrightTimeout:
                print(f"      TIMEOUT — skipping")
            except Exception as e:
                print(f"      ERROR: {e}")
            finally:
                queue.task_done()

        browser.close()

    print(f"\nDone. {total[0]} pages saved to ./{output_dir}/")


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="IBM Docs scraper")

    parser.add_argument("--seed", metavar="URL", action="append", default=None,
        help="Seed URL to crawl. Can pass multiple times for multiple seeds.")
    parser.add_argument("--output-dir", "-o", default="output",
        help="Base output directory for new run folders (default: output/)")
    parser.add_argument("--delay", "-d", type=float, default=1.0,
        help="Seconds between requests (default: 1.0)")
    parser.add_argument("--limit", "-l", type=int, default=None,
        help="Stop after N pages — useful for test runs")
    parser.add_argument("--resume", "-r", action="store_true",
        help="Resume from checkpoint after interrupted crawl")
    parser.add_argument("--checkpoint", default=CHECKPOINT_FILE,
        help=f"Checkpoint filename inside output-dir (default: {CHECKPOINT_FILE})")
    parser.add_argument("--rewrite-links", action="store_true",
        help="Rewrite IBM URLs in .md files to local relative links. No pages fetched.")

    args = parser.parse_args()

    if args.rewrite_links:
        rewrite_links(Path(args.output_dir))
        return

    if args.seed is None:
        parser.error("--seed is required unless you are using --rewrite-links")

    crawl(args)


if __name__ == "__main__":
    main()
