"""
tools/open_website.py — Fetch and scrape web pages for the agent.

Two-layer fetching strategy:
  1. httpx + BeautifulSoup  — fast, lightweight, works for most static sites
  2. Playwright + stealth   — full headless browser for JS-heavy or bot-protected
                              sites; triggered automatically when layer 1 yields
                              an 'app' or 'blocked' result, or explicitly via
                              the 'javascript' parameter.

Session persistence: cookies and auth state are saved per-domain under
data/open_website/sessions/{domain}.json so the agent can log in once and
stay logged in across calls.

Cache: fully processed page data is cached under data/open_website/{hash}.json.
Expired entries are purged at the start of each call using filesystem mtime —
no background jobs needed.

────────────────────────────────────────────────────────────────────────────────
Parameters accepted by the LLM:

  url                    str   required  — URL to fetch
  chunk                  int   default 1 — which chunk to return (1-based)
  refresh                bool            — force re-fetch even if cached
  javascript             bool            — force Playwright (skip httpx attempt)
  chunk_size_chars       int             — override YAML default
  include_links          bool            — override YAML default
  include_external_links bool            — override YAML default
  selector               str             — CSS selector to scope content extraction
  wait_for               str             — CSS selector to wait for before scraping
                                           (Playwright only — useful for lazy-loaded
                                           content; e.g. 'main', '#content')
  session_id             str             — domain key for persisted cookie session;
                                           defaults to the page's domain

────────────────────────────────────────────────────────────────────────────────
Page types returned:

  content  — substantial prose/text (article, docs, product page)
  index    — mostly links/navigation, low text density
  app      — JS-heavy, little static text; Playwright auto-triggered
  blocked  — login wall, paywall, CAPTCHA, or access denied

────────────────────────────────────────────────────────────────────────────────
Result payload:

  {
    "url":             "https://example.com/page",
    "title":           "Page Title",
    "page_type":       "content",
    "fetch_method":    "httpx" | "playwright",
    "summary":         "First substantive paragraph…",
    "content":         "## Heading\\n\\nParagraph…",
    "chunk":           1,
    "total_chunks":    3,
    "cached":          false,
    "session_saved":   true,
    "internal_links":  [{"text": "…", "url": "…", "hint": "navigation|article|pagination|link"}],
    "external_links":  [],
    "forms":           [{"action": "…", "method": "post", "fields": ["email","password"]}],
    "instructions":    "…",
  }
"""

import os
import re
import time
import json
import hashlib
from urllib.parse import urlparse, urljoin

from core.tool_base import ToolBase

TOOL_NAME = 'open_website'
log = ToolBase.logger(TOOL_NAME)

from typing import Annotated
from pydantic import Field


# ── Schema ────────────────────────────────────────────────────────────────────

def open_website(
    url: Annotated[str, Field(
        description="The full URL to open. If no scheme is given, https:// is assumed.",
    )],
    chunk: Annotated[int, Field(
        default=1,
        description="Which chunk of content to return (1-based). Call again with chunk=2, 3 etc. to page through long pages.",
    )] = 1,
    refresh: Annotated[bool, Field(
        default=False,
        description="Set true to force a fresh fetch, ignoring any cached version of the page.",
    )] = False,
    javascript: Annotated[bool, Field(
        default=False,
        description="Set true to use a full headless browser (Playwright). Use for JS-heavy apps, sites that require interaction, or when a previous attempt returned page_type 'app' or 'blocked'.",
    )] = False,
    selector: Annotated[str, Field(
        default="",
        description="CSS selector to scope extraction to a specific part of the page (e.g. 'article', '#main-content', '.post-body'). Leave empty to extract the full page.",
    )] = "",
    wait_for: Annotated[str, Field(
        default="",
        description="CSS selector to wait for before extracting content (Playwright only). Useful for lazy-loaded content — e.g. 'main', '#results'.",
    )] = "",
    include_links: Annotated[bool, Field(
        default=True,
        description="Whether to include internal_links in the result. Set false to reduce payload size when links are not needed.",
    )] = True,
    include_external_links: Annotated[bool, Field(
        default=False,
        description="Whether to include external links in the result. Off by default to reduce noise.",
    )] = False,
    session_id: Annotated[str, Field(
        default="",
        description="Key for persisted cookie session. Defaults to the page domain. Set explicitly to share a session across different URLs on the same site (e.g. after logging in).",
    )] = "",
    chunk_size_chars: Annotated[int, Field(
        default=0,
        description="Override the default chunk size in characters. Leave 0 to use the config default (4000). Increase for denser pages, decrease for faster responses.",
    )] = 0,
) -> str:
    """
    Open a URL and return its content as clean markdown, ready to read or summarise.

    Use this tool whenever the user asks you to open, visit, read, check, look up,
    or browse a website or URL. Also use it to follow links discovered on a previous
    page, or to dig deeper into a site after seeing its navigation structure.

    The result includes:
    - page_type: 'content' (readable), 'index' (navigation/links), 'app' (JS-heavy), 'blocked' (auth wall)
    - content: the page text as markdown, in chunks if the page is long
    - internal_links: links within the same domain, each labelled with a hint ('navigation', 'article', 'pagination', 'link')
    - forms: any forms on the page (login boxes, search fields etc.)
    - summary: a short digest of the first substantive paragraph

    Workflow for navigating a site:
    1. Fetch the root URL — if page_type is 'index', review internal_links and fetch the most relevant one.
    2. If page_type is 'app' or 'blocked', retry with javascript=true.
    3. If content is long (total_chunks > 1), page through with chunk=2, chunk=3 etc.
    4. Use selector to target a specific section if the page has a lot of noise around the content you need.
    """
    ...



# ── Constants ─────────────────────────────────────────────────────────────────

MIN_CONTENT_WORDS  = 120   # below this word count → likely index or app
LINK_DENSITY_HIGH  = 0.5   # internal links / words ratio above this → index
SCRIPT_TAG_HEAVY   = 8     # script tags above this with low text → app

BLOCKED_PHRASES = [
    'sign in to continue', 'log in to continue', 'create an account to',
    'subscribe to continue', 'subscribe to read', 'access denied',
    '403 forbidden', 'please verify you are human', 'enable javascript to',
    'complete the captcha', 'just a moment', 'checking your browser',
]

# Browser-like headers for httpx
BROWSER_HEADERS = {
    'User-Agent':                'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                                 'AppleWebKit/537.36 (KHTML, like Gecko) '
                                 'Chrome/125.0.0.0 Safari/537.36',
    'Accept':                    'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language':           'en-US,en;q=0.9',
    'Accept-Encoding':           'gzip, deflate, br',
    'DNT':                       '1',
    'Upgrade-Insecure-Requests': '1',
    'Sec-Fetch-Dest':            'document',
    'Sec-Fetch-Mode':            'navigate',
    'Sec-Fetch-Site':            'none',
    'Sec-Fetch-User':            '?1',
}


# ── Entry point ───────────────────────────────────────────────────────────────

def execute(tool_args, session, core, tool_config):
    params = ToolBase.params(tool_args)

    # ── Resolve URL ───────────────────────────────────────────────────────────
    url = params.get('url', '').strip()
    if not url:
        return ToolBase.error(core, TOOL_NAME, "No URL provided.")
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url

    domain = urlparse(url).netloc

    # ── Resolve options ───────────────────────────────────────────────────────
    chunk_num        = int(params.get('chunk', 1))
    force_refresh    = bool(params.get('refresh', False))
    force_js         = bool(params.get('javascript', False))
    chunk_size       = int(params.get('chunk_size_chars',
                           tool_config.get('chunk_size_chars', 4000)))
    include_links    = bool(params.get('include_links',
                            tool_config.get('include_links', True)))
    include_external = bool(params.get('include_external_links',
                            tool_config.get('include_external_links', False)))
    css_selector     = params.get('selector', None)
    wait_for         = params.get('wait_for', None)
    session_id       = params.get('session_id', domain)
    timeout          = int(tool_config.get('request_timeout_seconds', 20))
    auto_js_fallback = bool(tool_config.get('auto_js_fallback', True))
    cache_ttl        = int(tool_config.get('cache_ttl_minutes', 60)) * 60

    # ── Cache purge ───────────────────────────────────────────────────────────
    _purge_expired_cache(cache_ttl)

    # ── Cache lookup ──────────────────────────────────────────────────────────
    cache_key  = _cache_key(url, css_selector)
    cache_file = f"{cache_key}.json"
    cached     = False
    page_data  = None

    if not force_refresh:
        page_data = ToolBase.read_json(TOOL_NAME, cache_file)
        if page_data:
            cached = True
            log.info(f"Cache hit: {url}")

    # ── Fetch ─────────────────────────────────────────────────────────────────
    fetch_method  = None
    session_saved = False

    if page_data is None:
        ToolBase.speak(core, session, f"Opening {domain}...")

        if force_js:
            # Agent explicitly requested browser
            log.info(f"Playwright (forced): {url}")
            ToolBase.speak(core, session, "Using full browser for this site.")
            try:
                html, final_url, cookies = _fetch_playwright(
                    url, timeout, wait_for, session_id
                )
                fetch_method  = 'playwright'
                session_saved = _save_session(session_id, cookies)
            except Exception as e:
                log.error(f"Playwright failed: {e}", exc_info=True)
                return ToolBase.error(core, TOOL_NAME, f"Browser fetch failed: {e}")
        else:
            # Try httpx first
            log.info(f"httpx: {url}")
            try:
                cookies_in   = _load_session(session_id)
                html, final_url, cookies_out = _fetch_httpx(url, timeout, cookies_in)
                fetch_method = 'httpx'
                if cookies_out:
                    session_saved = _save_session(session_id, cookies_out)
            except Exception as e:
                log.warning(f"httpx failed ({e}), trying Playwright")
                html, final_url, cookies_out = None, url, {}

            # Auto-fallback to Playwright if needed
            if html is None or (auto_js_fallback and _looks_like_needs_browser(html or '')):
                log.info(f"Playwright (auto-fallback): {url}")
                ToolBase.speak(core, session, "Site needs a full browser, switching now.")
                try:
                    html, final_url, cookies = _fetch_playwright(
                        url, timeout, wait_for, session_id
                    )
                    fetch_method  = 'playwright'
                    session_saved = _save_session(session_id, cookies)
                except Exception as e:
                    if html is None:
                        log.error(f"Both fetch methods failed: {e}", exc_info=True)
                        return ToolBase.error(core, TOOL_NAME, f"Failed to fetch URL: {e}")
                    # httpx got something, use it even if imperfect
                    log.warning(f"Playwright fallback also failed, using httpx result: {e}")
                    fetch_method = 'httpx'

        page_data = _process(html, final_url, css_selector, include_links, include_external)
        page_data['fetch_method'] = fetch_method
        page_data['fetched_at']   = time.time()
        ToolBase.write_json(TOOL_NAME, cache_file, page_data)

    # ── Debug logging ─────────────────────────────────────────────────────────
    if tool_config.get('debug_log_content', False):
        _debug_log_page(page_data, url, tool_config)

    # ── Chunk ─────────────────────────────────────────────────────────────────
    full_content  = page_data.get('content', '')
    chunks        = _split_chunks(full_content, chunk_size)
    total_chunks  = max(len(chunks), 1)
    chunk_idx     = max(1, min(chunk_num, total_chunks))
    chunk_content = chunks[chunk_idx - 1] if chunks else ''

    # ── Result ────────────────────────────────────────────────────────────────
    page_type    = page_data.get('page_type', 'content')
    instructions = _build_instructions(page_type, chunk_idx, total_chunks,
                                       page_data.get('fetch_method', 'httpx'))
    result = {
        "url":           page_data.get('url', url),
        "title":         page_data.get('title', ''),
        "page_type":     page_type,
        "fetch_method":  page_data.get('fetch_method', fetch_method or 'httpx'),
        "summary":       page_data.get('summary', ''),
        "content":       chunk_content,
        "chunk":         chunk_idx,
        "total_chunks":  total_chunks,
        "cached":        cached,
        "session_saved": session_saved,
        "forms":         page_data.get('forms', []),
        "instructions":  instructions,
    }
    if include_links:
        result["internal_links"] = page_data.get('internal_links', [])
    if include_external:
        result["external_links"] = page_data.get('external_links', [])

    return ToolBase.result(core, TOOL_NAME, result)


# ── httpx fetch ───────────────────────────────────────────────────────────────

def _fetch_httpx(url: str, timeout: int,
                 cookies_in: dict) -> tuple[str, str, dict]:
    """
    Fetch with httpx. Returns (html, final_url, cookies_out).
    Raises on network errors.
    """
    import httpx

    # http2=True requires the 'h2' package (httpx[http2]); fall back gracefully
    try:
        import h2  # noqa: F401
        use_http2 = True
    except ImportError:
        use_http2 = False

    with httpx.Client(
        follow_redirects = True,
        timeout          = timeout,
        headers          = BROWSER_HEADERS,
        cookies          = cookies_in,
        http2            = use_http2,
    ) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.text, str(resp.url), dict(resp.cookies)


def _looks_like_needs_browser(html: str) -> bool:
    """
    Heuristic: does this HTML look like it needs JS to be useful?
    Used to decide whether to auto-trigger Playwright fallback.
    """
    if not html or len(html.strip()) < 500:
        return True

    lower = html.lower()

    # Cloudflare / bot detection pages
    cf_signals = ['just a moment', 'checking your browser', 'cf-browser-verification',
                  'enable javascript and cookies', 'ray id']
    if any(s in lower for s in cf_signals):
        return True

    # Almost no visible text despite having HTML
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, 'lxml')
    for tag in soup(['script', 'style', 'noscript']):
        tag.decompose()
    text = soup.get_text(' ', strip=True)
    if len(text.split()) < 50:
        return True

    return False


# ── Playwright fetch ──────────────────────────────────────────────────────────

def _wait_through_challenge(page, wait_for: str | None, timeout: int):
    """
    Wait for the real page content after potential bot-protection challenges
    (Cloudflare, DataDome, etc.).

    Strategy:
    1. Check immediately if we're on a challenge page.
    2. If so, wait up to 15s for the challenge to resolve — Cloudflare's JS
       challenge typically solves itself in 3-6s and then redirects.
    3. After any challenge clears, wait for the user-specified selector or
       fall back to networkidle with a generous timeout.
    """
    CHALLENGE_SIGNALS = [
        'just a moment',
        'checking your browser',
        'please wait',
        'cf-browser-verification',
        'challenge-running',
        'ray id',
        '_cf_chl',
    ]
    CHALLENGE_TIMEOUT_MS = 15000
    POLL_INTERVAL_MS     = 500

    def _is_challenge(p) -> bool:
        try:
            body = p.inner_text('body', timeout=2000).lower()
            return any(s in body for s in CHALLENGE_SIGNALS)
        except Exception:
            return False

    # Wait through the challenge if we landed on one
    if _is_challenge(page):
        log.info("Bot challenge detected — waiting for it to resolve")
        elapsed = 0
        while elapsed < CHALLENGE_TIMEOUT_MS:
            page.wait_for_timeout(POLL_INTERVAL_MS)
            elapsed += POLL_INTERVAL_MS
            if not _is_challenge(page):
                log.info(f"Challenge resolved after ~{elapsed}ms")
                break
        else:
            log.warning("Challenge did not resolve within timeout — proceeding anyway")

    # Now wait for the actual content
    if wait_for:
        try:
            page.wait_for_selector(wait_for, timeout=10000)
        except Exception:
            log.warning(f"wait_for selector '{wait_for}' not found — continuing anyway")
    else:
        try:
            page.wait_for_load_state('networkidle', timeout=10000)
        except Exception:
            pass  # networkidle can time out on chatty pages — that's fine


def _fetch_playwright(url: str, timeout: int, wait_for: str | None,
                      session_id: str) -> tuple[str, str, list]:
    """
    Fetch with a headless Chromium browser via Playwright + stealth.
    Returns (html, final_url, cookies).
    cookies is a list of Playwright cookie dicts for session persistence.
    """
    from playwright.sync_api import sync_playwright

    try:
        from playwright_stealth import Stealth
        _stealth = Stealth()
        has_stealth = True
    except ImportError:
        _stealth = None
        has_stealth = False
        log.warning("playwright_stealth not available — bot detection bypass disabled")

    saved_cookies = _load_session(session_id)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            viewport          = {'width': 1280, 'height': 800},
            user_agent        = BROWSER_HEADERS['User-Agent'],
            locale            = 'en-US',
            timezone_id       = 'America/New_York',
            java_script_enabled = True,
        )

        # Restore saved session cookies if any
        if saved_cookies:
            try:
                context.add_cookies(saved_cookies)
            except Exception as e:
                log.warning(f"Could not restore session cookies: {e}")

        page = context.new_page()

        if has_stealth:
            _stealth.apply_stealth_sync(page)

        page.goto(url, timeout=timeout * 1000, wait_until='domcontentloaded')

        # Handle Cloudflare and similar JS challenges — they redirect the browser
        # after solving, so we wait for the URL to stabilise or a known challenge
        # element to disappear, then wait for the real page to load.
        _wait_through_challenge(page, wait_for, timeout)

        html       = page.content()
        final_url  = page.url
        cookies    = context.cookies()

        browser.close()

    return html, final_url, cookies


# ── Session persistence ───────────────────────────────────────────────────────

def _sessions_dir() -> str:
    """Return the sessions directory, creating it if needed."""
    path = os.path.join(ToolBase.data_path(TOOL_NAME), 'sessions')
    os.makedirs(path, exist_ok=True)
    return path


def _session_path(session_id: str) -> str:
    """Return the full path to a session file."""
    safe = re.sub(r'[^a-zA-Z0-9._-]', '_', session_id)
    return os.path.join(_sessions_dir(), f"{safe}.json")


def _load_session(session_id: str) -> list:
    """Load saved cookies for this session_id. Returns [] if none."""
    path = _session_path(session_id)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except FileNotFoundError:
        return []
    except Exception as e:
        log.warning(f"Could not load session {session_id}: {e}")
        return []


def _save_session(session_id: str, cookies) -> bool:
    """
    Persist cookies. Accepts either:
      - list of Playwright cookie dicts  (from Playwright fetch)
      - dict of name→value              (from httpx)
    Always stores as Playwright format (list of dicts) for maximum reuse.
    Writes directly to the sessions directory, bypassing ToolBase.write_json
    to avoid intermediate-directory issues with atomic tmp writes.
    """
    if not cookies:
        return False

    if isinstance(cookies, dict):
        # Convert httpx flat dict to minimal Playwright format
        cookies = [{'name': k, 'value': v} for k, v in cookies.items()]

    path = _session_path(session_id)
    tmp  = path + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(cookies, f, indent=2)
        os.replace(tmp, path)
        return True
    except Exception as e:
        log.error(f"Failed to save session {session_id}: {e}")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False


# ── HTML → structured data ────────────────────────────────────────────────────

def _process(html: str, url: str, selector: str | None,
             include_links: bool, include_external: bool) -> dict:
    from bs4 import BeautifulSoup

    base_domain = urlparse(url).netloc
    soup        = BeautifulSoup(html, 'lxml')

    # Count scripts before decomposing anything
    script_count = len(soup.find_all('script'))

    # Scope to selector if given — full CSS selector support via BS4
    if selector:
        scoped = soup.select_one(selector)
        if scoped:
            soup = BeautifulSoup(str(scoped), 'lxml')
        else:
            log.warning(f"Selector '{selector}' not found on page")

    title          = _extract_title(soup)
    internal_links = []
    external_links = []
    forms          = []

    if include_links or include_external:
        internal_links, external_links = _extract_links(soup, url, base_domain)

    forms = _extract_forms(soup, url)

    markdown   = _soup_to_markdown(soup)
    page_type  = _detect_page_type(markdown, internal_links, script_count, title, html)
    summary    = _make_summary(markdown, page_type)

    return {
        "url":            url,
        "title":          title,
        "page_type":      page_type,
        "summary":        summary,
        "content":        markdown,
        "internal_links": internal_links if include_links    else [],
        "external_links": external_links if include_external else [],
        "forms":          forms,
    }


def _extract_title(soup) -> str:
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    h1 = soup.find('h1')
    if h1:
        return h1.get_text(' ', strip=True)
    return ''


def _soup_to_markdown(soup) -> str:
    """
    Walk the BS4 tree and produce clean markdown.
    Removes boilerplate (nav, footer, aside, ads) first.
    """
    from bs4 import NavigableString, Tag

    # Remove noise elements entirely
    for tag in soup.find_all(['script', 'style', 'noscript', 'svg',
                               'iframe', 'nav', 'footer', 'aside',
                               'header', 'form', 'button', 'figure']):
        tag.decompose()

    # Remove hidden elements
    for tag in soup.find_all(style=re.compile(r'display\s*:\s*none', re.I)):
        tag.decompose()
    for tag in soup.find_all(attrs={'aria-hidden': 'true'}):
        tag.decompose()

    def walk(node) -> str:
        if isinstance(node, NavigableString):
            return str(node)

        if not isinstance(node, Tag):
            return ''

        name     = node.name.lower() if node.name else ''
        children = ''.join(walk(c) for c in node.children)

        if name in ('h1','h2','h3','h4','h5','h6'):
            level = int(name[1])
            text  = children.strip()
            return f"\n\n{'#' * level} {text}\n\n" if text else ''

        if name == 'p':
            text = children.strip()
            return f"\n\n{text}\n\n" if text else ''

        if name in ('ul', 'ol'):
            return f"\n{children}\n"

        if name == 'li':
            text = children.strip()
            return f"- {text}\n" if text else ''

        if name in ('strong', 'b'):
            text = children.strip()
            return f"**{text}**" if text else ''

        if name in ('em', 'i'):
            text = children.strip()
            return f"*{text}*" if text else ''

        if name == 'a':
            href = node.get('href', '')
            text = children.strip()
            if href and text:
                return f"[{text}]({href})"
            return text

        if name == 'img':
            alt = node.get('alt', '').strip()
            src = node.get('src', '')
            return f"![{alt}]({src})" if alt else ''

        if name in ('blockquote',):
            lines = children.strip().splitlines()
            return '\n' + '\n'.join(f"> {l}" for l in lines) + '\n'

        if name in ('code',):
            return f"`{children}`"

        if name in ('pre',):
            return f"\n```\n{children.strip()}\n```\n"

        if name == 'br':
            return '\n'

        if name == 'hr':
            return '\n---\n'

        if name in ('table',):
            return _table_to_markdown(node)

        if name in ('div', 'section', 'article', 'main', 'span',
                    'td', 'th', 'tr', 'tbody', 'thead',
                    'html', 'body', 'head', '[document]'):
            return children

        # Unknown tags — pass through children
        return children

    raw = walk(soup)

    # Clean up whitespace
    raw = re.sub(r'\n{3,}', '\n\n', raw)
    raw = re.sub(r'[ \t]+', ' ', raw)
    raw = '\n'.join(line.strip() for line in raw.splitlines())
    raw = re.sub(r'\n{3,}', '\n\n', raw)
    return raw.strip()


def _table_to_markdown(table_tag) -> str:
    """Convert an HTML table to a markdown table."""
    rows = []
    for tr in table_tag.find_all('tr'):
        cells = [td.get_text(' ', strip=True) for td in tr.find_all(['th', 'td'])]
        rows.append(cells)

    if not rows:
        return ''

    col_count = max(len(r) for r in rows)
    # Pad rows
    rows = [r + [''] * (col_count - len(r)) for r in rows]

    lines   = []
    header  = rows[0]
    lines.append('| ' + ' | '.join(header) + ' |')
    lines.append('| ' + ' | '.join(['---'] * col_count) + ' |')
    for row in rows[1:]:
        lines.append('| ' + ' | '.join(row) + ' |')

    return '\n\n' + '\n'.join(lines) + '\n\n'


def _extract_links(soup, base_url: str, base_domain: str) -> tuple[list, list]:
    internal = []
    external = []
    seen     = set()

    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        text = a.get_text(' ', strip=True)

        if not href or not text:
            continue
        if href.startswith(('mailto:', 'tel:', 'javascript:', '#')):
            continue

        abs_url = urljoin(base_url, href)
        if abs_url in seen:
            continue
        seen.add(abs_url)

        parsed   = urlparse(abs_url)
        is_inner = parsed.netloc == base_domain or parsed.netloc == ''
        hint     = _link_hint(abs_url, text)
        entry    = {"text": text[:120], "url": abs_url, "hint": hint}

        if is_inner:
            internal.append(entry)
        else:
            external.append(entry)

    return internal[:80], external[:40]


def _link_hint(url: str, text: str) -> str:
    url_l  = url.lower()
    text_l = text.lower()

    nav_words  = {'home', 'about', 'contact', 'login', 'sign in', 'register',
                  'menu', 'search', 'help', 'faq', 'pricing', 'blog', 'news',
                  'support', 'docs', 'documentation', 'careers', 'terms', 'privacy'}
    page_words = {'next', 'previous', 'prev', 'older', 'newer', '»', '«', 'load more'}

    if any(w in text_l for w in page_words):
        return 'pagination'
    if any(w in text_l for w in nav_words):
        return 'navigation'
    if re.search(r'/\d{4}/\d{2}/', url_l) or re.search(r'article|post|story|blog', url_l):
        return 'article'
    return 'link'


def _extract_forms(soup, base_url: str) -> list:
    """Extract form details so the agent knows what input the page expects."""
    forms = []
    for form in soup.find_all('form'):
        action = urljoin(base_url, form.get('action', ''))
        method = form.get('method', 'get').lower()
        fields = []
        for inp in form.find_all(['input', 'textarea', 'select']):
            name      = inp.get('name') or inp.get('id') or inp.get('placeholder', '')
            inp_type  = inp.get('type', 'text')
            if name and inp_type not in ('hidden', 'submit', 'button', 'reset'):
                fields.append(name)
        if fields:
            forms.append({"action": action, "method": method, "fields": fields})
    return forms[:10]


# ── Page type detection ───────────────────────────────────────────────────────

def _detect_page_type(text: str, internal_links: list,
                      script_count: int, title: str, html: str) -> str:
    combined = (text + ' ' + title).lower()
    words    = text.split()

    # Blocked / auth wall — check before other types
    block_hits = sum(1 for p in BLOCKED_PHRASES if p in combined)
    if block_hits >= 1 and len(words) < 300:
        return 'blocked'

    # App — JS heavy, almost no extracted text
    if script_count > SCRIPT_TAG_HEAVY and len(words) < MIN_CONTENT_WORDS:
        return 'app'

    # Index — low word count or high link density
    if len(words) < MIN_CONTENT_WORDS:
        return 'index'

    link_density = len(internal_links) / max(len(words), 1)
    if link_density > LINK_DENSITY_HIGH:
        return 'index'

    return 'content'


# ── Summary ───────────────────────────────────────────────────────────────────

def _make_summary(text: str, page_type: str) -> str:
    if page_type in ('app', 'blocked'):
        return ''
    for para in text.split('\n\n'):
        para = para.strip()
        if para.startswith('#') or para.startswith('-') or para.startswith('|'):
            continue
        words = para.split()
        if len(words) >= 20:
            snippet = ' '.join(words[:80])
            return snippet + ('…' if len(words) > 80 else '')
    return ''


# ── Chunking ──────────────────────────────────────────────────────────────────

def _split_chunks(text: str, chunk_size: int) -> list[str]:
    if not text:
        return ['']
    if len(text) <= chunk_size:
        return [text]

    chunks = []
    start  = 0
    length = len(text)

    while start < length:
        end = start + chunk_size
        if end >= length:
            chunks.append(text[start:].strip())
            break
        # Prefer paragraph break, then sentence, then hard cut
        boundary = text.rfind('\n\n', start, end)
        if boundary <= start:
            boundary = text.rfind('. ', start, end)
        if boundary <= start:
            boundary = end
        chunks.append(text[start:boundary].strip())
        start = boundary

    return [c for c in chunks if c.strip()]


# ── Debug logging ─────────────────────────────────────────────────────────────────────────────────

def _debug_log_page(page_data: dict, url: str, tool_config: dict):
    """
    Log scraped page content when debug_log_content: true in YAML.
    Truncates to debug_log_max_chars to avoid flooding the log.
    """
    max_chars = int(tool_config.get('debug_log_max_chars', 2000))
    content   = page_data.get('content', '')
    truncated = len(content) > max_chars

    log.debug(
        f"\n{'-' * 60}\n"
        f"open_website debug — {page_data.get('page_type', '?').upper()}"
        f" via {page_data.get('fetch_method', '?')}\n"
        f"URL:   {url}\n"
        f"Title: {page_data.get('title', '(none)')}\n"
        f"Links: {len(page_data.get('internal_links', []))} internal, "
        f"{len(page_data.get('external_links', []))} external\n"
        f"Forms: {len(page_data.get('forms', []))}\n"
        f"{'-' * 60}\n"
        f"{content[:max_chars]}"
        + (f"\n… [{len(content) - max_chars:,} chars truncated]" if truncated else '')
        + f"\n{'-' * 60}"
    )


# ── Cache ─────────────────────────────────────────────────────────────────────

def _cache_key(url: str, selector: str | None = None) -> str:
    key = url + (selector or '')
    return hashlib.md5(key.encode('utf-8')).hexdigest()


def _purge_expired_cache(ttl_seconds: float):
    """Remove stale .json cache files using filesystem mtime. Sessions are exempt."""
    try:
        dir_path = ToolBase.data_path(TOOL_NAME)
        now      = time.time()
        for fname in os.listdir(dir_path):
            if not fname.endswith('.json'):
                continue
            fpath = os.path.join(dir_path, fname)
            try:
                if now - os.path.getmtime(fpath) > ttl_seconds:
                    os.remove(fpath)
                    log.info(f"Purged expired cache: {fname}")
            except Exception:
                pass
    except Exception as e:
        log.warning(f"Cache purge failed: {e}")


# ── LLM instructions ──────────────────────────────────────────────────────────

def _build_instructions(page_type: str, chunk: int,
                        total_chunks: int, method: str) -> str:
    parts = []

    if page_type == 'blocked':
        parts.append(
            "This page is behind a login wall, paywall, or bot-protection. "
            "Inform the user the content could not be accessed. "
            "If there are forms present, offer to describe what credentials or "
            "input the page is asking for. "
            "If the fetch_method was 'httpx', you may suggest retrying with "
            "javascript=true to use the full browser."
        )
    elif page_type == 'app':
        parts.append(
            "This appears to be a JavaScript-heavy web application — "
            "extracted static content may be incomplete. "
            if method == 'httpx' else
            "This is a JS-heavy app; the browser rendered what it could. "
            "Content may still be limited if the app requires interaction."
        )
    elif page_type == 'index':
        parts.append(
            "This is a navigation or listing page rather than content. "
            "Review the internal_links to identify the most relevant deeper page "
            "for the user's goal and offer to fetch it. "
            "Look for 'article' and 'link' hints over 'navigation' hints."
        )
    else:
        parts.append("Present the content clearly and helpfully to the user.")

    if total_chunks > 1:
        if chunk < total_chunks:
            parts.append(
                f"This is chunk {chunk} of {total_chunks}. "
                f"Summarise what you've seen so far and offer to continue to chunk {chunk + 1}."
            )
        else:
            parts.append(
                f"This is the final chunk ({chunk} of {total_chunks}). "
                "Provide a complete summary of the full page content."
            )

    return ' '.join(parts)