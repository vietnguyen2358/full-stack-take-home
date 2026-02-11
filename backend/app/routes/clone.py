import asyncio
import base64
import json
import logging
import os
import re
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import certifi
import httpx

# Fix SSL cert verification on macOS (Python can't find system certs)
os.environ.setdefault("SSL_CERT_FILE", certifi.where())
os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
from daytona import CreateSandboxFromImageParams, Daytona, DaytonaConfig
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from playwright.async_api import async_playwright
from pydantic import BaseModel, HttpUrl

from app.database import get_supabase

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_HTML_CHARS = 200_000
TEMPLATE_DIR = Path(__file__).resolve().parent.parent.parent / "sandbox-template"

# Files to upload from the template (relative to TEMPLATE_DIR)
TEMPLATE_FILES = [
    "package.json",
    "next.config.ts",
    "tsconfig.json",
    "postcss.config.mjs",
    "src/lib/utils.ts",
    "src/app/layout.tsx",
    "src/app/globals.css",
    "src/app/[...slug]/page.tsx",
]


class CloneRequest(BaseModel):
    url: HttpUrl


VIEWPORT_WIDTH = 1280
VIEWPORT_HEIGHT = 900
MAX_SCREENSHOTS = 15  # increased for long pages
MAX_IMAGE_URLS = 100
MAX_STRUCTURED_ELEMENTS = 300
MAX_BUILD_ATTEMPTS = 3


def strip_markdown_fences(text: str) -> str:
    """Remove markdown code fences and any preamble before the actual code."""
    text = text.strip()
    # Remove markdown code fences
    if text.startswith("```"):
        first_newline = text.index("\n")
        text = text[first_newline + 1:]
    if text.endswith("```"):
        text = text[:-3].rstrip()
    # Strip any preamble text before the actual code.
    # Look for // FILE: marker first (multi-file output)
    file_marker_idx = text.find("// FILE:")
    if file_marker_idx != -1:
        text = text[file_marker_idx:]
    else:
        # Single-file: must start with "use client" or an import statement.
        for marker in ['"use client"', "'use client'", "import "]:
            idx = text.find(marker)
            if idx != -1:
                text = text[idx:]
                break
    return text.strip()


def parse_multi_file_output(raw: str) -> dict[str, str]:
    """Split AI output on '// FILE: <path>' markers into {path: content}.

    If no markers found, treat entire output as src/app/page.tsx (backward compat).
    """
    if "// FILE:" not in raw:
        return {"src/app/page.tsx": raw}

    files: dict[str, str] = {}
    parts = raw.split("// FILE:")
    for part in parts:
        part = part.strip()
        if not part:
            continue
        newline_idx = part.find("\n")
        if newline_idx == -1:
            continue
        path = part[:newline_idx].strip()
        content = part[newline_idx + 1:].strip()
        if path and content:
            files[path] = content
    return files



def _clean_html(html: str) -> str:
    """Strip noise from HTML while preserving SVGs for logo/icon fidelity.

    1. Extract all <svg>...</svg> blocks and replace with placeholders.
    2. Remove <script>, <style>, <noscript> tags and their contents.
    3. Remove HTML comments.
    4. Remove data-* and event handler attributes.
    5. Simplify inline styles (keep only layout-critical properties).
    6. Collapse excessive whitespace.
    7. Restore SVGs (but truncate absurdly long path data).
    """
    # 1. Extract SVGs
    svgs: list[str] = []

    def _stash_svg(m: re.Match) -> str:
        svg = m.group(0)
        # Truncate very long SVG path data (>500 chars per path)
        svg = re.sub(
            r'(\s+d="[^"]{500})[^"]*"',
            r'\1..."',
            svg,
        )
        idx = len(svgs)
        svgs.append(svg)
        return f"<!--SVG_PLACEHOLDER_{idx}-->"

    cleaned = re.sub(r"<svg[\s\S]*?</svg>", _stash_svg, html, flags=re.IGNORECASE)

    # 2. Remove <script>, <style>, <noscript> with content
    for tag in ("script", "style", "noscript"):
        cleaned = re.sub(
            rf"<{tag}[\s\S]*?</{tag}>", "", cleaned, flags=re.IGNORECASE
        )

    # 3. Remove HTML comments (but not our SVG placeholders)
    cleaned = re.sub(r"<!--(?!SVG_PLACEHOLDER_)\s*[\s\S]*?-->", "", cleaned)

    # 4. Remove data-* and event handler attributes
    cleaned = re.sub(r'\s+data-[\w-]+="[^"]*"', "", cleaned)
    cleaned = re.sub(r"\s+data-[\w-]+=\'[^']*\'", "", cleaned)
    cleaned = re.sub(r'\s+on\w+="[^"]*"', "", cleaned)

    # 5. Remove non-essential attributes (aria-*, role is sometimes useful but verbose)
    cleaned = re.sub(r'\s+aria-[\w-]+="[^"]*"', "", cleaned)

    # 6. Collapse whitespace
    cleaned = re.sub(r"\n\s*\n+", "\n", cleaned)
    cleaned = re.sub(r"  +", " ", cleaned)

    # 7. Restore SVGs
    for i, svg in enumerate(svgs):
        cleaned = cleaned.replace(f"<!--SVG_PLACEHOLDER_{i}-->", svg)

    return cleaned.strip()


# JavaScript to extract computed styles from the page
_JS_EXTRACT_STYLES = """() => {
    const result = {};

    // CSS custom properties from :root
    const rootStyles = getComputedStyle(document.documentElement);
    const customProps = {};
    for (const sheet of document.styleSheets) {
        try {
            for (const rule of sheet.cssRules) {
                if (rule.selectorText === ':root' || rule.selectorText === ':root, :host') {
                    for (let i = 0; i < rule.style.length; i++) {
                        const prop = rule.style[i];
                        if (prop.startsWith('--')) {
                            customProps[prop] = rule.style.getPropertyValue(prop).trim();
                        }
                    }
                }
            }
        } catch(e) {} // cross-origin sheets
    }
    result.cssVariables = customProps;

    // Font families from prominent elements
    const fonts = new Set();
    for (const sel of ['body', 'h1', 'h2', 'h3', 'p', 'a', 'button', 'nav']) {
        const el = document.querySelector(sel);
        if (el) fonts.add(getComputedStyle(el).fontFamily);
    }
    result.fonts = [...fonts];

    // Key colors
    const body = document.body;
    const bodyStyle = getComputedStyle(body);
    result.bodyBg = bodyStyle.backgroundColor;
    result.bodyColor = bodyStyle.color;

    // Nav/header colors
    const header = document.querySelector('header, nav, [role="banner"]');
    if (header) {
        const hs = getComputedStyle(header);
        result.headerBg = hs.backgroundColor;
        result.headerColor = hs.color;
    }

    // Footer colors
    const footer = document.querySelector('footer, [role="contentinfo"]');
    if (footer) {
        const fs = getComputedStyle(footer);
        result.footerBg = fs.backgroundColor;
        result.footerColor = fs.color;
    }

    // Primary button colors (first button with a bg)
    const btns = document.querySelectorAll('button, a.btn, [role="button"]');
    for (const btn of btns) {
        const bs = getComputedStyle(btn);
        if (bs.backgroundColor && bs.backgroundColor !== 'rgba(0, 0, 0, 0)') {
            result.primaryBtnBg = bs.backgroundColor;
            result.primaryBtnColor = bs.color;
            break;
        }
    }

    return result;
}"""

# JavaScript to extract structured content in DOM order
_JS_EXTRACT_CONTENT = """(maxElements) => {
    const items = [];
    const selectors = 'h1, h2, h3, h4, h5, h6, p, a, button, label, img, li, span.hero, [role="heading"]';
    const elements = document.querySelectorAll(selectors);

    for (const el of elements) {
        if (items.length >= maxElements) break;
        const tag = el.tagName.toLowerCase();
        const text = el.textContent?.trim().substring(0, 200);
        if (!text && tag !== 'img') continue;

        const item = { tag };
        if (tag === 'img') {
            item.src = el.src || '';
            item.alt = el.alt || '';
        } else if (tag === 'a') {
            item.text = text;
            item.href = el.href || '';
        } else {
            item.text = text;
        }
        items.push(item);
    }
    return items;
}"""


# JavaScript to extract navigation/menu structures including dropdowns
_JS_EXTRACT_NAV = """() => {
    const navs = [];
    const navEls = document.querySelectorAll('nav, [role="navigation"], header');

    for (const nav of navEls) {
        const navItem = { items: [] };

        // Find top-level menu items and their dropdowns
        const topLinks = nav.querySelectorAll(':scope > ul > li, :scope > div > ul > li, :scope > div > div > a, :scope > ul > li > a');
        const seen = new Set();

        for (const li of nav.querySelectorAll('li, [role="menuitem"]')) {
            const link = li.querySelector('a') || li;
            const text = link.textContent?.trim().substring(0, 100);
            if (!text || seen.has(text)) continue;
            seen.add(text);

            const menuItem = { label: text };

            // Check for nested dropdown items
            const subItems = li.querySelectorAll('ul li a, [role="menu"] a, [role="menuitem"]');
            if (subItems.length > 0) {
                menuItem.dropdown = [];
                const subSeen = new Set();
                for (const sub of subItems) {
                    const subText = sub.textContent?.trim().substring(0, 100);
                    if (subText && !subSeen.has(subText) && subText !== text) {
                        subSeen.add(subText);
                        menuItem.dropdown.push(subText);
                    }
                }
                if (menuItem.dropdown.length === 0) delete menuItem.dropdown;
            }

            navItem.items.push(menuItem);
        }

        if (navItem.items.length > 0) navs.push(navItem);
    }
    return navs;
}"""

# JavaScript to extract carousel/slider/tab content (including hidden slides)
_JS_EXTRACT_INTERACTIVE = """() => {
    const results = [];

    // Common carousel/slider selectors
    const carouselSelectors = [
        '[class*="carousel"]', '[class*="slider"]', '[class*="swiper"]',
        '[class*="slide"]', '[data-carousel]', '[data-slider]',
        '[role="tabpanel"]', '[class*="testimonial"]',
        '[class*="card-stack"]', '[class*="rotating"]'
    ];

    const containers = document.querySelectorAll(carouselSelectors.join(', '));
    const seen = new Set();

    for (const container of containers) {
        // Skip if this is a child of an already-processed carousel
        if (seen.has(container) || [...seen].some(s => s.contains(container))) continue;

        // Find all slide-like children
        const slideSelectors = [
            ':scope > div', ':scope > li', ':scope > article',
            '[class*="slide"]', '[role="tabpanel"]', '[class*="item"]'
        ];
        let slides = [];
        for (const sel of slideSelectors) {
            const found = container.querySelectorAll(sel);
            if (found.length > 1) { slides = [...found]; break; }
        }
        if (slides.length < 2) continue;

        seen.add(container);
        const carousel = {
            type: container.className.includes('tab') ? 'tabs' : 'carousel',
            selector: container.className.split(' ').filter(c => c.length > 2).slice(0, 3).join('.'),
            slideCount: slides.length,
            slides: []
        };

        for (const slide of slides.slice(0, 20)) {
            const slideData = {};
            // Get heading
            const h = slide.querySelector('h1, h2, h3, h4, h5, h6');
            if (h) slideData.title = h.textContent?.trim().substring(0, 200);
            // Get description
            const p = slide.querySelector('p');
            if (p) slideData.description = p.textContent?.trim().substring(0, 300);
            // Get image
            const img = slide.querySelector('img');
            if (img) { slideData.image = img.src; slideData.alt = img.alt; }
            // Get link text
            const a = slide.querySelector('a');
            if (a) slideData.linkText = a.textContent?.trim().substring(0, 100);
            // Get any remaining visible text
            if (!slideData.title && !slideData.description) {
                slideData.text = slide.textContent?.trim().substring(0, 300);
            }
            if (Object.keys(slideData).length > 0) carousel.slides.push(slideData);
        }

        if (carousel.slides.length >= 2) results.push(carousel);
    }

    // Also find tab groups
    const tabLists = document.querySelectorAll('[role="tablist"]');
    for (const tabList of tabLists) {
        const tabs = tabList.querySelectorAll('[role="tab"]');
        if (tabs.length < 2) continue;
        const tabGroup = { type: 'tabs', slideCount: tabs.length, slides: [] };
        for (const tab of tabs) {
            const panelId = tab.getAttribute('aria-controls');
            const panel = panelId ? document.getElementById(panelId) : null;
            const tabData = { title: tab.textContent?.trim() };
            if (panel) {
                const ph = panel.querySelector('h1, h2, h3, h4, h5, h6');
                if (ph) tabData.panelTitle = ph.textContent?.trim().substring(0, 200);
                const pp = panel.querySelector('p');
                if (pp) tabData.panelDescription = pp.textContent?.trim().substring(0, 300);
                const pi = panel.querySelector('img');
                if (pi) { tabData.image = pi.src; tabData.alt = pi.alt; }
            }
            tabGroup.slides.push(tabData);
        }
        results.push(tabGroup);
    }

    return results;
}"""


async def _call_ai(client: httpx.AsyncClient, messages: list[dict], api_key: str) -> str:
    """Send messages to OpenRouter and return the assistant's response content."""
    resp = await client.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": "anthropic/claude-sonnet-4.5",
            "messages": messages,
            "max_tokens": 64000,
        },
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def _sse_event(data: dict) -> str:
    """Format a dict as an SSE data line."""
    return f"data: {json.dumps(data)}\n\n"


def _log(msg: str) -> str:
    """Shorthand: emit an SSE log event."""
    return _sse_event({"log": msg})


def _status(status: str, message: str) -> str:
    """Shorthand: emit an SSE status event."""
    return _sse_event({"status": status, "message": message})


def _db_insert_clone(url: str) -> str | None:
    """Insert a new clone record and return its ID, or None if DB is not configured."""
    sb = get_supabase()
    if not sb:
        return None
    try:
        result = sb.table("clones").insert({"url": url, "status": "scraping"}).execute()
        return result.data[0]["id"] if result.data else None
    except Exception as e:
        logger.warning("[db] Failed to insert clone: %s", e)
        return None


def _db_update_clone(clone_id: str | None, **fields) -> None:
    """Update a clone record. No-op if clone_id is None or DB not configured."""
    if not clone_id:
        return
    sb = get_supabase()
    if not sb:
        return
    try:
        sb.table("clones").update(fields).eq("id", clone_id).execute()
    except Exception as e:
        logger.warning("[db] Failed to update clone %s: %s", clone_id, e)


@router.post("/clone")
async def clone_website(req: CloneRequest):
    """Scrape a URL and stream progress via SSE, then return the AI-generated clone."""
    url_str = str(req.url)
    request_start = time.time()
    logger.info("=== CLONE REQUEST START === url=%s", url_str)
    clone_id = _db_insert_clone(url_str)

    async def event_stream():
        try:
            # ── SCRAPING ──────────────────────────────────────────
            scrape_start = time.time()
            yield _status("scraping", "Scraping website...")
            yield _log(f"Target URL: {url_str}")
            yield _log("Launching headless browser (Chromium)...")
            await asyncio.sleep(0)

            try:
                async with async_playwright() as p:
                    browser = await p.chromium.launch(headless=True)
                    logger.info("[scrape] Browser launched for %s", url_str)
                    yield _log(f"Browser launched — viewport {VIEWPORT_WIDTH}x{VIEWPORT_HEIGHT}")
                    page = await browser.new_page(
                        viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
                        user_agent=(
                            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/131.0.0.0 Safari/537.36"
                        ),
                    )

                    yield _log(f"Navigating to {url_str}...")
                    await asyncio.sleep(0)
                    nav_start = time.time()
                    await page.goto(url_str, wait_until="domcontentloaded", timeout=30000)
                    await page.wait_for_timeout(3000)
                    logger.info("[scrape] Page loaded in %.1fs — %s", time.time() - nav_start, url_str)
                    yield _log("Page loaded (domcontentloaded + 3s render wait)")

                    # First pass: scroll to bottom to trigger all lazy-loaded content
                    yield _log("Scrolling page to trigger lazy-loaded content...")
                    await asyncio.sleep(0)
                    scroll_start = time.time()
                    scroll_count = 0
                    prev_height = 0
                    for _ in range(30):  # safety limit
                        total_height = await page.evaluate("document.body.scrollHeight")
                        if total_height == prev_height:
                            break
                        prev_height = total_height
                        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        await page.wait_for_timeout(800)
                        scroll_count += 1
                    # Scroll back to top
                    await page.evaluate("window.scrollTo(0, 0)")
                    await page.wait_for_timeout(500)
                    logger.info("[scrape] Lazy-load scroll: %d scrolls in %.1fs, final height=%dpx", scroll_count, time.time() - scroll_start, prev_height)
                    yield _log("Lazy-load scroll complete")

                    # Now capture HTML after all content is loaded
                    raw_html = await page.content()

                    # Clean HTML: strip scripts/styles/noise, preserve SVGs
                    clean_start = time.time()
                    html = _clean_html(raw_html)
                    reduction = 100 - len(html) * 100 // max(len(raw_html), 1)
                    logger.info("[scrape] HTML cleaned: %d → %d chars (%d%% reduction) in %.2fs", len(raw_html), len(html), reduction, time.time() - clean_start)
                    yield _log(f"Cleaned HTML: {len(raw_html):,} chars → {len(html):,} chars ({reduction}% reduction)")

                    # Extract computed styles (exact colors, fonts)
                    yield _log("Extracting computed styles...")
                    await asyncio.sleep(0)
                    computed_styles: dict = await page.evaluate(_JS_EXTRACT_STYLES)
                    logger.info("[scrape] Computed styles: %d fonts, %d CSS vars, bodyBg=%s", len(computed_styles.get("fonts", [])), len(computed_styles.get("cssVariables", {})), computed_styles.get("bodyBg", "n/a"))
                    yield _log(f"Got styles — {len(computed_styles.get('fonts', []))} font families, {len(computed_styles.get('cssVariables', {}))} CSS vars")

                    # Extract structured content (DOM-order outline)
                    yield _log("Extracting structured content...")
                    structured_content: list[dict] = await page.evaluate(
                        _JS_EXTRACT_CONTENT, MAX_STRUCTURED_ELEMENTS
                    )
                    logger.info("[scrape] Structured content: %d elements", len(structured_content))
                    yield _log(f"Extracted {len(structured_content)} content elements")

                    # Extract navigation/menu structures (including dropdowns)
                    yield _log("Extracting navigation structure...")
                    nav_structure: list[dict] = await page.evaluate(_JS_EXTRACT_NAV)
                    total_dropdown_items = sum(
                        len(item.get("dropdown", []))
                        for nav in nav_structure
                        for item in nav.get("items", [])
                    )
                    logger.info("[scrape] Navigation: %d nav(s), %d dropdown items", len(nav_structure), total_dropdown_items)
                    yield _log(f"Found {len(nav_structure)} nav(s) with {total_dropdown_items} dropdown items")

                    # Extract carousel/slider/tab content (including hidden slides)
                    yield _log("Extracting interactive elements (carousels, sliders, tabs)...")
                    interactive_elements: list[dict] = await page.evaluate(_JS_EXTRACT_INTERACTIVE)
                    total_slides = sum(el.get("slideCount", 0) for el in interactive_elements)
                    logger.info("[scrape] Interactive elements: %d groups, %d total slides", len(interactive_elements), total_slides)
                    yield _log(f"Found {len(interactive_elements)} interactive element(s) with {total_slides} total slides/tabs")

                    yield _log("Extracting image URLs...")
                    await asyncio.sleep(0)
                    image_urls: list[str] = await page.evaluate("""(maxUrls) => {
                        const urls = new Set();
                        document.querySelectorAll('img[src]').forEach(img => {
                            if (img.src) urls.add(img.src);
                        });
                        document.querySelectorAll('*').forEach(el => {
                            const bg = getComputedStyle(el).backgroundImage;
                            const match = bg.match(/url\\(["']?(https?:\\/\\/[^"')]+)["']?\\)/);
                            if (match) urls.add(match[1]);
                        });
                        document.querySelectorAll('source[srcset]').forEach(src => {
                            src.srcset.split(',').forEach(s => {
                                const u = s.trim().split(/\\s+/)[0];
                                if (u.startsWith('http')) urls.add(u);
                            });
                        });
                        document.querySelectorAll('link[rel*="icon"][href]').forEach(link => {
                            if (link.href) urls.add(link.href);
                        });
                        return [...urls].slice(0, maxUrls);
                    }""", MAX_IMAGE_URLS)
                    logger.info("[scrape] Found %d image URLs", len(image_urls))
                    yield _log(f"Found {len(image_urls)} image/asset URLs")

                    total_height = await page.evaluate("document.body.scrollHeight")
                    yield _log(f"Page height: {total_height}px — taking viewport screenshots...")
                    await asyncio.sleep(0)

                    screenshots: list[str] = []
                    offset = 0
                    while offset < total_height and len(screenshots) < MAX_SCREENSHOTS:
                        await page.evaluate(f"window.scrollTo(0, {offset})")
                        await page.wait_for_timeout(600)
                        shot = await page.screenshot(full_page=False)
                        screenshots.append(base64.b64encode(shot).decode("utf-8"))
                        offset += VIEWPORT_HEIGHT
                        yield _log(f"  Screenshot {len(screenshots)} captured (offset {offset}px)")

                    await browser.close()
                    scrape_elapsed = time.time() - scrape_start
                    screenshot_bytes = sum(len(s) for s in screenshots)
                    logger.info("[scrape] COMPLETE in %.1fs — %d screenshots (%.1fMB base64), %d images, page height=%dpx", scrape_elapsed, len(screenshots), screenshot_bytes / 1_048_576, len(image_urls), total_height)
                    yield _log(f"Scraping complete — {len(screenshots)} screenshots captured")
                    _db_update_clone(clone_id, status="generating", screenshot_count=len(screenshots), image_count=len(image_urls), html_raw_size=len(raw_html), html_cleaned_size=len(html))
            except Exception as e:
                logger.error("[scrape] FAILED for %s: %s\n%s", url_str, e, traceback.format_exc())
                _db_update_clone(clone_id, status="error", error_message=f"Scrape failed: {e}")
                raise HTTPException(status_code=422, detail=f"Failed to scrape page: {e}")

            # ── GENERATING ────────────────────────────────────────
            gen_start = time.time()
            yield _status("generating", "Generating clone with AI...")
            yield _log(f"Preparing prompt with {len(screenshots)} screenshots + {len(image_urls)} image URLs...")
            await asyncio.sleep(0)

            api_key = os.environ.get("OPENROUTER_API_KEY")
            if not api_key:
                logger.error("[generate] OPENROUTER_API_KEY is not set")
                raise HTTPException(status_code=500, detail="OPENROUTER_API_KEY is not set")

            truncated_html = html[:MAX_HTML_CHARS]
            image_list = "\n".join(f"  - {u}" for u in image_urls) if image_urls else "  (none found)"
            n = len(screenshots)

            # Format computed styles for the prompt
            styles_section = ""
            if computed_styles:
                style_lines = []
                if computed_styles.get("fonts"):
                    style_lines.append(f"Font families: {', '.join(computed_styles['fonts'])}")
                if computed_styles.get("bodyBg"):
                    style_lines.append(f"Body background: {computed_styles['bodyBg']}")
                if computed_styles.get("bodyColor"):
                    style_lines.append(f"Body text color: {computed_styles['bodyColor']}")
                if computed_styles.get("headerBg"):
                    style_lines.append(f"Header background: {computed_styles['headerBg']}")
                if computed_styles.get("headerColor"):
                    style_lines.append(f"Header text color: {computed_styles['headerColor']}")
                if computed_styles.get("footerBg"):
                    style_lines.append(f"Footer background: {computed_styles['footerBg']}")
                if computed_styles.get("footerColor"):
                    style_lines.append(f"Footer text color: {computed_styles['footerColor']}")
                if computed_styles.get("primaryBtnBg"):
                    style_lines.append(f"Primary button background: {computed_styles['primaryBtnBg']}")
                if computed_styles.get("primaryBtnColor"):
                    style_lines.append(f"Primary button text: {computed_styles['primaryBtnColor']}")
                css_vars = computed_styles.get("cssVariables", {})
                if css_vars:
                    # Include up to 30 most useful CSS variables
                    var_lines = [f"  {k}: {v}" for k, v in list(css_vars.items())[:30]]
                    style_lines.append("CSS custom properties:\n" + "\n".join(var_lines))
                styles_section = "\n".join(style_lines)

            # Format structured content for the prompt
            content_outline = ""
            if structured_content:
                outline_lines = []
                for item in structured_content:
                    tag = item.get("tag", "")
                    text = item.get("text", "")
                    if tag == "img":
                        outline_lines.append(f"  [{tag}] src={item.get('src', '')} alt=\"{item.get('alt', '')}\"")
                    elif tag == "a":
                        outline_lines.append(f"  [{tag}] \"{text}\" href={item.get('href', '')}")
                    else:
                        outline_lines.append(f"  [{tag}] \"{text}\"")
                content_outline = "\n".join(outline_lines)

            # Format navigation structure for the prompt
            nav_section = ""
            if nav_structure:
                nav_lines = []
                for i, nav in enumerate(nav_structure):
                    nav_lines.append(f"  Navigation {i + 1}:")
                    for item in nav.get("items", []):
                        label = item.get("label", "")
                        dropdown = item.get("dropdown", [])
                        if dropdown:
                            nav_lines.append(f"    [{label}] ▼ dropdown: {', '.join(dropdown)}")
                        else:
                            nav_lines.append(f"    [{label}]")
                nav_section = "\n".join(nav_lines)

            # Format interactive elements for the prompt
            interactive_section = ""
            if interactive_elements:
                int_lines = []
                for i, el in enumerate(interactive_elements):
                    el_type = el.get("type", "carousel")
                    slide_count = el.get("slideCount", 0)
                    int_lines.append(f"  {el_type.upper()} #{i + 1} ({slide_count} items):")
                    for j, slide in enumerate(el.get("slides", [])):
                        parts = []
                        if slide.get("title"):
                            parts.append(f'title="{slide["title"]}"')
                        if slide.get("description"):
                            parts.append(f'desc="{slide["description"][:150]}"')
                        if slide.get("text"):
                            parts.append(f'text="{slide["text"][:150]}"')
                        if slide.get("image"):
                            parts.append(f'img={slide["image"]}')
                        if slide.get("alt"):
                            parts.append(f'alt="{slide["alt"]}"')
                        if slide.get("linkText"):
                            parts.append(f'link="{slide["linkText"]}"')
                        if slide.get("panelTitle"):
                            parts.append(f'panelTitle="{slide["panelTitle"]}"')
                        if slide.get("panelDescription"):
                            parts.append(f'panelDesc="{slide["panelDescription"][:150]}"')
                        int_lines.append(f"    Slide {j + 1}: {', '.join(parts)}")
                interactive_section = "\n".join(int_lines)

            prompt = (
                "You are a pixel-perfect website cloning expert. Your goal is to produce a 1:1 visual replica.\n"
                "Given the HTML source, computed styles, a content outline, "
                f"and {n} sequential screenshots capturing the ENTIRE page (scrolled top to bottom), "
                "generate a Next.js page component (page.tsx) that is visually IDENTICAL to the original.\n\n"
                "CRITICAL — SCREENSHOT FIDELITY:\n"
                "The screenshots are your PRIMARY reference. Study EVERY screenshot carefully.\n"
                "- Each screenshot shows a viewport-sized slice of the page from top to bottom.\n"
                "- You MUST reproduce EVERY section visible in EVERY screenshot. Count the screenshots and verify\n"
                "  your output covers all of them. If there are 13 screenshots, your page must have 13 screenshots\n"
                "  worth of content — do NOT summarize or skip sections.\n"
                "- Match the EXACT layout: grid columns, flex directions, spacing, padding, margins, border-radius.\n"
                "- Match EXACT font sizes, font weights, letter-spacing, line-height from what you see.\n"
                "- Match EXACT colors — backgrounds, text, borders, gradients, shadows.\n"
                "- Reproduce decorative elements: gradient overlays, glows, blurs, grid patterns, dot patterns.\n"
                "- Your output will be LONG. That is expected and correct. Do not try to be concise.\n\n"
                "Tech stack pre-installed in the project:\n"
                "- React 19 with Next.js 16 App Router\n"
                "- Tailwind CSS (available for styling)\n"
                "- lucide-react icons — import { IconName } from \"lucide-react\"\n"
                "- framer-motion — import { motion, AnimatePresence } from \"framer-motion\" for animations\n"
                "- Utility: import { cn } from \"@/lib/utils\" (className merge helper)\n"
                "- No pre-built UI component library is installed, but you can import and use ANY npm package\n"
                "  (e.g. @radix-ui, @headlessui, react-icons, @mui/material, chakra-ui, etc.) — npm install runs before build.\n"
                "  You may also use Tailwind classes, inline styles, CSS modules, or any combination.\n\n"
                "EXACT COMPUTED STYLES (use these exact values, do NOT guess from screenshots):\n"
                f"{styles_section}\n\n"
                "STRUCTURED CONTENT OUTLINE (elements in DOM order — use for exact text and ordering):\n"
                f"{content_outline}\n\n"
                "NAVIGATION STRUCTURE (menus with their dropdown items — implement ALL of these as functional dropdowns):\n"
                f"{nav_section if nav_section else '  (no dropdowns detected)'}\n\n"
                "INTERACTIVE ELEMENTS DATA (carousels, sliders, tabs — ALL slides/items extracted, including hidden ones):\n"
                f"{interactive_section if interactive_section else '  (none detected)'}\n\n"
                "Rules:\n"
                "- OUTPUT FORMAT: You may output multiple files using '// FILE: <path>' markers.\n"
                "  At minimum output src/app/page.tsx. Break large pages into components for structure:\n"
                "  // FILE: src/app/page.tsx\n"
                "  \"use client\";\n"
                "  import { Navbar } from \"@/components/navbar\";\n"
                "  ...\n"
                "  // FILE: src/components/navbar.tsx\n"
                "  ...\n"
                "  Extract navbar, footer, and major repeated sections into separate component files.\n"
                "  Import custom components from \"@/components/<name>\" (maps to src/components/<name>.tsx).\n"
                "  No markdown fences, no explanation — just the raw code.\n"
                '- page.tsx MUST start with "use client" and export a default function component.\n'
                "- CRITICAL: The code must be valid TypeScript/JSX with no syntax errors. "
                "Ensure all brackets, braces, and parentheses are properly closed. "
                "Test mentally that the component compiles before outputting.\n"
                "- BACKGROUND COLOR — THIS IS CRITICAL: The project has NO default background color. "
                "You MUST set the background color on your outermost wrapper div using the exact body background color from the computed styles above. "
                "For dark-themed sites, this means the entire page must have a dark background — there should be NO white gaps or white sections "
                "unless the original site actually has white sections. Use a single wrapper: "
                "<div className=\"min-h-screen\" style={{ backgroundColor: '...', color: '...' }}> with the exact body background and text colors. "
                "Every section must either inherit this background or set its own explicit background color.\n"
                "- EXACT TEXT REPRODUCTION: You MUST copy ALL text content EXACTLY as it appears in the HTML source "
                "and the structured content outline — "
                "company names, brand names, headings, paragraphs, button labels, nav links, footer text, etc. "
                "NEVER substitute, rename, or invent company names or branding. "
                "Read the text from the HTML/outline and use it verbatim.\n"
                "- CONTENT ORDER: Follow the structured content outline above for the correct ordering of elements. "
                "This outline shows the exact DOM order of headings, paragraphs, links, buttons, and images.\n"
                "- LOGOS & BRANDING: Reproduce logos exactly. Use the original image URL for logo images. "
                "For inline SVG logos in the HTML, copy the SVG paths exactly — do NOT replace them with generic icons. "
                "The clone must look like the SAME company's website, not a different one.\n"
                "- COLORS: Use the exact computed color values provided above (body, header, footer, button colors). "
                "Apply these exact color values using Tailwind arbitrary values (e.g. bg-[rgb(255,255,255)]), inline styles, or CSS variables. "
                "Text colors must also match — use the computed foreground colors, not defaults.\n"
                "- FONTS: Use the exact font families from the computed styles. "
                "For Google Fonts, add a <link> tag via useEffect.\n"
                "- Use lucide-react for icons that match the original site (for decorative icons only, NOT for logos).\n"
                "- IMAGES: Use the original image URLs with regular <img> tags (NOT next/image). Here are the image URLs extracted:\n"
                f"{image_list}\n"
                "  Use these exact URLs. Match sizing and position from the screenshots.\n"
                "  If an image URL is not listed, check the HTML source for it.\n"
                "  For SVG logos/icons inlined in the HTML, reproduce them as inline SVGs exactly as in the HTML.\n"
                "- LINKS: This is a visual clone, NOT a functional website. All <a> tags must use href=\"#\" "
                "and e.preventDefault() so clicking them does nothing. Do NOT link to external URLs or other pages. "
                "The clone should be fully self-contained — users can interact with UI elements (dropdowns, carousels, tabs) "
                "but should never navigate away from the page.\n"
                "- You may use React hooks (useState, useEffect, useRef, etc.) for interactivity and animations.\n"
                "- DROPDOWN MENUS: Check the NAVIGATION STRUCTURE section above. Every menu item marked with ▼ dropdown "
                "MUST be implemented as a working dropdown. Use useState to track which dropdown is open, toggle on click, "
                "and show/hide the dropdown panel with all the listed sub-items. Style the dropdown as an absolute-positioned "
                "panel below the trigger. Close dropdowns when clicking outside (useEffect with document click listener).\n"
                "- INTERACTIVE ELEMENTS: Check the INTERACTIVE ELEMENTS DATA section above. "
                "Carousels, sliders, image rotators, tabs, accordions, and any interactive components "
                "MUST be functional React components with real state and transitions — not static snapshots. "
                "The extracted data includes ALL slides/items (including hidden ones not visible in screenshots). "
                "You MUST include EVERY slide/tab from the extracted data — not just the one visible in the screenshot. "
                "Use useState for slide index, useEffect with setInterval for auto-advancing carousels, "
                "onClick handlers for navigation arrows/dots, and framer-motion AnimatePresence for slide transitions. "
                "Each carousel/slider must show prev/next controls and indicator dots matching the original design.\n"
                "- For animations, use framer-motion, CSS transitions, or CSS keyframe animations.\n"
                "- FULL PAGE — EVERY SECTION: You MUST reproduce the ENTIRE page from top to bottom. "
                f"There are {n} screenshots showing the full page. Go through each screenshot one by one and make sure "
                "every visible section is in your output. Typical sections include: navbar, hero, features, "
                "use cases, testimonials/logos, pricing, CTA, footer. Do NOT stop early or skip any section.\n"
                "- PIXEL-PERFECT STYLING: Match exact spacing (padding, margin, gap), exact font sizes and weights, "
                "exact border-radius values, exact colors (backgrounds, text, borders, gradients). "
                "Use inline styles for precise values when Tailwind classes aren't exact enough.\n"
                "- GRADIENTS & DECORATIVE EFFECTS: Reproduce all gradient backgrounds, glow effects, "
                "backdrop-blur, box-shadows, text-gradients, and decorative patterns visible in the screenshots.\n"
                "- LENGTH: Your output should be LONG — typically 500-1500+ lines for a full landing page. "
                "If your output is under 300 lines, you are almost certainly skipping sections.\n\n"
                f"Here is the cleaned page HTML (scripts/styles removed, SVGs preserved):\n\n{truncated_html}"
            )

            content: list[dict] = [{"type": "text", "text": prompt}]
            for i, shot_b64 in enumerate(screenshots):
                content.append({"type": "text", "text": f"Screenshot {i + 1} of {n}:"})
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{shot_b64}"},
                })

            prompt_chars = len(prompt)
            logger.info("[generate] Prompt size: %d chars, %d screenshots, %d image URLs, HTML truncated to %d chars", prompt_chars, len(screenshots), len(image_urls), len(truncated_html))
            yield _log("Sending request to claude-sonnet-4.5 via OpenRouter...")
            await asyncio.sleep(0)

            ai_messages = [{"role": "user", "content": content}]

            ai_start = time.time()
            async with httpx.AsyncClient(timeout=300) as client:
                try:
                    raw_response = await _call_ai(client, ai_messages, api_key)
                except httpx.HTTPStatusError as e:
                    logger.error("[generate] OpenRouter HTTP error: %d — %s", e.response.status_code, e.response.text[:500])
                    raise HTTPException(
                        status_code=502,
                        detail=f"OpenRouter API error: {e.response.status_code} — {e.response.text}",
                    )
                except httpx.RequestError as e:
                    logger.error("[generate] OpenRouter request error: %s", e)
                    raise HTTPException(
                        status_code=502, detail=f"Failed to reach OpenRouter: {e}"
                    )
                except (KeyError, IndexError):
                    logger.error("[generate] Unexpected OpenRouter response format")
                    raise HTTPException(status_code=502, detail="Unexpected OpenRouter response format")

            ai_elapsed = time.time() - ai_start
            cleaned_response = strip_markdown_fences(raw_response)
            generated_files = parse_multi_file_output(cleaned_response)
            generated_code = generated_files.get("src/app/page.tsx", "")
            logger.info("[generate] AI responded in %.1fs — %d files generated (%s)", ai_elapsed, len(generated_files), ", ".join(generated_files.keys()))
            yield _log(f"AI generated {len(generated_files)} file(s): {', '.join(generated_files.keys())}")

            gen_elapsed = time.time() - gen_start
            logger.info("[generate] COMPLETE in %.1fs (AI call: %.1fs)", gen_elapsed, ai_elapsed)
            _db_update_clone(clone_id, status="deploying", generated_code=generated_code)

            # ── DEPLOYING ─────────────────────────────────────────
            deploy_start = time.time()
            yield _status("deploying", "Creating sandbox...")
            yield _log("Creating Daytona sandbox (node:20)...")
            await asyncio.sleep(0)

            preview_url = None
            try:
                sandbox_start = time.time()
                sandbox = await asyncio.to_thread(_create_sandbox)
                if sandbox is None:
                    logger.warning("[deploy] DAYTONA_API_KEY not set — skipping deployment")
                    yield _log("DAYTONA_API_KEY not set — skipping deployment")
                else:
                    logger.info("[deploy] Sandbox created in %.1fs", time.time() - sandbox_start)
                    yield _log("Sandbox created successfully")
                    project_dir = "/home/daytona/app"

                    # Upload files
                    yield _status("deploying", "Uploading project files...")
                    # Compute all directories needed
                    all_dirs = {"src/app/[...slug]", "src/lib"}
                    for gen_path in generated_files:
                        parent = str(Path(gen_path).parent)
                        if parent and parent != ".":
                            all_dirs.add(parent)
                    mkdir_cmd = " ".join(f"{project_dir}/{d}" for d in sorted(all_dirs))
                    yield _log("Creating project directory structure...")
                    await asyncio.to_thread(
                        sandbox.process.exec,
                        f"mkdir -p {mkdir_cmd}"
                    )

                    upload_start = time.time()
                    for rel_path in TEMPLATE_FILES:
                        yield _log(f"  Uploading {rel_path}")
                        file_path = TEMPLATE_DIR / rel_path
                        await asyncio.to_thread(
                            sandbox.fs.upload_file,
                            file_path.read_bytes(),
                            f"{project_dir}/{rel_path}",
                        )

                    # Upload all AI-generated files
                    for gen_path, gen_content in generated_files.items():
                        yield _log(f"  Uploading {gen_path} (generated)")
                        await asyncio.to_thread(
                            sandbox.fs.upload_file,
                            gen_content.encode("utf-8"),
                            f"{project_dir}/{gen_path}",
                        )
                    logger.info("[deploy] Uploaded %d template + %d generated files in %.1fs", len(TEMPLATE_FILES), len(generated_files), time.time() - upload_start)

                    # npm install
                    yield _status("deploying", "Installing dependencies...")
                    yield _log("Running npm install...")
                    await asyncio.sleep(0)
                    npm_start = time.time()
                    await asyncio.to_thread(
                        sandbox.process.exec,
                        f"cd {project_dir} && npm install",
                    )
                    logger.info("[deploy] npm install completed in %.1fs", time.time() - npm_start)
                    yield _log("npm install complete")

                    # ── BUILD CHECK WITH RETRY ────────────────────────
                    build_ok = False
                    for attempt in range(1, MAX_BUILD_ATTEMPTS + 1):
                        yield _status("deploying", f"Building project (attempt {attempt}/{MAX_BUILD_ATTEMPTS})...")
                        yield _log(f"Running next build (attempt {attempt}/{MAX_BUILD_ATTEMPTS})...")
                        await asyncio.sleep(0)

                        # Re-upload all generated files before each retry
                        if attempt > 1:
                            yield _log("  Re-uploading fixed files...")
                            for gen_path, gen_content in generated_files.items():
                                await asyncio.to_thread(
                                    sandbox.fs.upload_file,
                                    gen_content.encode("utf-8"),
                                    f"{project_dir}/{gen_path}",
                                )

                        build_start = time.time()
                        build_result = await asyncio.to_thread(
                            sandbox.process.exec,
                            f"cd {project_dir} && npx next build 2>&1",
                        )

                        build_output = build_result.result or ""
                        build_elapsed = time.time() - build_start
                        if build_result.exit_code == 0:
                            logger.info("[deploy] Build succeeded on attempt %d in %.1fs", attempt, build_elapsed)
                            yield _log("Build succeeded!")
                            build_ok = True
                            break

                        # Build failed — truncate error output for the fix prompt
                        error_text = build_output[-3000:] if len(build_output) > 3000 else build_output
                        logger.warning("[deploy] Build failed attempt %d/%d (exit %d) in %.1fs: %s", attempt, MAX_BUILD_ATTEMPTS, build_result.exit_code, build_elapsed, error_text[:300])
                        yield _log(f"Build failed (exit code {build_result.exit_code})")
                        yield _log(f"Error output:\n{error_text[:500]}")

                        if attempt < MAX_BUILD_ATTEMPTS:
                            yield _status("fixing", f"Asking AI to fix code (attempt {attempt + 1}/{MAX_BUILD_ATTEMPTS})...")
                            yield _log(f"Sending build errors to AI for fix (attempt {attempt + 1}/{MAX_BUILD_ATTEMPTS})...")
                            await asyncio.sleep(0)

                            multi_file_context = "\n\n".join(f"// FILE: {p}\n{c}" for p, c in generated_files.items())
                            fix_messages = [
                                {"role": "user", "content": content},
                                {"role": "assistant", "content": multi_file_context},
                                {"role": "user", "content": (
                                    "The code above failed to build with Next.js. Here is the build error output:\n\n"
                                    f"```\n{error_text}\n```\n\n"
                                    "Please fix the code and output ALL files using // FILE: <path> markers. "
                                    "No markdown fences, no explanation — just the raw code."
                                )},
                            ]

                            try:
                                fix_start = time.time()
                                async with httpx.AsyncClient(timeout=300) as fix_client:
                                    fix_response = await _call_ai(fix_client, fix_messages, api_key)
                                cleaned_fix = strip_markdown_fences(fix_response)
                                generated_files = parse_multi_file_output(cleaned_fix)
                                generated_code = generated_files.get("src/app/page.tsx", "")
                                logger.info("[deploy] AI fix attempt %d returned %d files in %.1fs", attempt + 1, len(generated_files), time.time() - fix_start)
                                yield _log(f"AI returned fixed code ({len(generated_files)} file(s))")
                            except Exception as fix_err:
                                logger.error("[deploy] AI fix request failed: %s", fix_err)
                                yield _log(f"AI fix request failed: {fix_err}")
                                break
                        else:
                            logger.warning("[deploy] All %d build attempts failed for %s", MAX_BUILD_ATTEMPTS, url_str)
                            yield _log(f"All {MAX_BUILD_ATTEMPTS} build attempts failed — proceeding anyway")

                    # Start dev server
                    yield _status("deploying", "Starting Next.js server...")
                    yield _log("Starting next dev on port 8080...")
                    await asyncio.to_thread(
                        sandbox.process.exec,
                        f"cd {project_dir} && nohup npx next dev -p 8080 > /tmp/next.log 2>&1 & disown",
                    )
                    yield _log("Waiting for server to be ready...")
                    await asyncio.sleep(5)

                    preview = sandbox.create_signed_preview_url(8080)
                    preview_url = preview.url
                    deploy_elapsed = time.time() - deploy_start
                    logger.info("[deploy] COMPLETE in %.1fs — preview: %s", deploy_elapsed, preview_url)
                    yield _log(f"Preview ready: {preview_url}")
            except Exception as e:
                logger.error("[deploy] FAILED for %s: %s\n%s", url_str, e, traceback.format_exc())
                yield _log(f"Deployment error: {e}")

            # ── DONE ──────────────────────────────────────────────
            # Build full project file map for the code viewer
            project_files: dict[str, str] = {}
            for rel_path in TEMPLATE_FILES:
                try:
                    project_files[rel_path] = (TEMPLATE_DIR / rel_path).read_text()
                except Exception:
                    pass
            for gen_path, gen_content in generated_files.items():
                project_files[gen_path] = gen_content

            total_elapsed = time.time() - request_start
            logger.info(
                "=== CLONE REQUEST COMPLETE === url=%s total=%.1fs code=%d chars preview=%s",
                url_str,
                total_elapsed,
                len(generated_code),
                preview_url or "none",
            )

            _db_update_clone(clone_id, status="done", preview_url=preview_url, generated_code=generated_code, completed_at=datetime.now(timezone.utc).isoformat())

            yield _sse_event({
                "status": "done",
                "code": generated_code,
                "preview_url": preview_url,
                "clone_id": clone_id,
                "files": project_files,
            })

        except HTTPException as e:
            logger.error("=== CLONE REQUEST FAILED === url=%s error=%s elapsed=%.1fs", url_str, e.detail, time.time() - request_start)
            _db_update_clone(clone_id, status="error", error_message=e.detail)
            yield _log(f"Error: {e.detail}")
            yield _sse_event({"status": "error", "message": e.detail})
        except Exception as e:
            logger.error("=== CLONE REQUEST FAILED === url=%s error=%s elapsed=%.1fs\n%s", url_str, e, time.time() - request_start, traceback.format_exc())
            _db_update_clone(clone_id, status="error", error_message=str(e))
            yield _log(f"Error: {e}")
            yield _sse_event({"status": "error", "message": str(e)})

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.get("/clones")
async def list_clones(limit: int = 20):
    """List recent clones, newest first."""
    sb = get_supabase()
    if not sb:
        return {"clones": [], "db_configured": False}
    try:
        result = (
            sb.table("clones")
            .select("id, url, status, preview_url, screenshot_count, image_count, created_at, completed_at")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return {"clones": result.data, "db_configured": True}
    except Exception as e:
        logger.error("[db] Failed to list clones: %s", e)
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


@router.get("/clones/{clone_id}")
async def get_clone(clone_id: str):
    """Get a single clone by ID, including full reconstructed file tree."""
    sb = get_supabase()
    if not sb:
        raise HTTPException(status_code=503, detail="Database not configured")
    try:
        result = sb.table("clones").select("*").eq("id", clone_id).single().execute()
        data = result.data
        # Reconstruct the full file tree from templates + stored generated code
        files: dict[str, str] = {}
        for rel_path in TEMPLATE_FILES:
            try:
                files[rel_path] = (TEMPLATE_DIR / rel_path).read_text()
            except Exception:
                pass
        if data.get("generated_code"):
            files["src/app/page.tsx"] = data["generated_code"]
        data["files"] = files
        return data
    except Exception as e:
        logger.error("[db] Failed to get clone %s: %s", clone_id, e)
        raise HTTPException(status_code=404, detail="Clone not found")


@router.delete("/clones/{clone_id}")
async def delete_clone(clone_id: str):
    """Delete a clone by ID."""
    sb = get_supabase()
    if not sb:
        raise HTTPException(status_code=503, detail="Database not configured")
    try:
        sb.table("clones").delete().eq("id", clone_id).execute()
        logger.info("[db] Deleted clone %s", clone_id)
        return {"ok": True}
    except Exception as e:
        logger.error("[db] Failed to delete clone %s: %s", clone_id, e)
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


@router.post("/clones/{clone_id}/redeploy")
async def redeploy_clone(clone_id: str):
    """Re-deploy a previously cloned site to a fresh Daytona sandbox."""
    sb = get_supabase()
    if not sb:
        raise HTTPException(status_code=503, detail="Database not configured")

    try:
        result = sb.table("clones").select("url, generated_code").eq("id", clone_id).single().execute()
        data = result.data
    except Exception as e:
        raise HTTPException(status_code=404, detail="Clone not found")

    if not data.get("generated_code"):
        raise HTTPException(status_code=400, detail="No generated code to deploy")

    generated_code = data["generated_code"]

    async def stream():
        try:
            yield _log("Re-deploying from saved code...")
            yield _status("deploying", "Creating Daytona sandbox...")
            yield _log("Creating Daytona sandbox (node:20)...")
            await asyncio.sleep(0)

            sandbox_start = time.time()
            sandbox = await asyncio.to_thread(_create_sandbox)
            if sandbox is None:
                yield _sse_event({"status": "error", "message": "DAYTONA_API_KEY not set"})
                return

            logger.info("[redeploy] Sandbox created in %.1fs for clone %s", time.time() - sandbox_start, clone_id)
            yield _log("Sandbox created successfully")
            project_dir = "/home/daytona/app"

            # Upload files
            yield _status("deploying", "Uploading project files...")
            await asyncio.to_thread(
                sandbox.process.exec,
                f"mkdir -p {project_dir}/src/app/[...slug] {project_dir}/src/lib"
            )

            upload_start = time.time()
            for rel_path in TEMPLATE_FILES:
                file_path = TEMPLATE_DIR / rel_path
                await asyncio.to_thread(
                    sandbox.fs.upload_file,
                    file_path.read_bytes(),
                    f"{project_dir}/{rel_path}",
                )

            await asyncio.to_thread(
                sandbox.fs.upload_file,
                generated_code.encode("utf-8"),
                f"{project_dir}/src/app/page.tsx",
            )
            logger.info("[redeploy] Uploaded files in %.1fs", time.time() - upload_start)
            yield _log("Files uploaded")

            # npm install
            yield _status("deploying", "Installing dependencies...")
            yield _log("Running npm install...")
            npm_start = time.time()
            await asyncio.to_thread(
                sandbox.process.exec,
                f"cd {project_dir} && npm install",
            )
            logger.info("[redeploy] npm install completed in %.1fs", time.time() - npm_start)
            yield _log("npm install complete")

            # Build
            yield _status("deploying", "Building project...")
            yield _log("Running next build...")
            build_start = time.time()
            build_result = await asyncio.to_thread(
                sandbox.process.exec,
                f"cd {project_dir} && npx next build 2>&1",
            )
            logger.info("[redeploy] Build finished in %.1fs (exit %d)", time.time() - build_start, build_result.exit_code)
            if build_result.exit_code != 0:
                yield _log("Build had errors — starting dev server anyway")

            # Start dev server
            yield _status("deploying", "Starting Next.js server...")
            yield _log("Starting next dev on port 8080...")
            await asyncio.to_thread(
                sandbox.process.exec,
                f"cd {project_dir} && nohup npx next dev -p 8080 > /tmp/next.log 2>&1 & disown",
            )
            await asyncio.sleep(5)

            preview = sandbox.create_signed_preview_url(8080)
            preview_url = preview.url
            logger.info("[redeploy] Preview ready: %s", preview_url)
            yield _log(f"Preview ready: {preview_url}")

            # Update the stored preview URL
            _db_update_clone(clone_id, preview_url=preview_url)

            yield _sse_event({"status": "done", "preview_url": preview_url})

        except Exception as e:
            logger.error("[redeploy] FAILED for clone %s: %s\n%s", clone_id, e, traceback.format_exc())
            yield _sse_event({"status": "error", "message": str(e)})

    return StreamingResponse(stream(), media_type="text/event-stream")


def _create_sandbox():
    """Create and return a Daytona sandbox, or None if not configured."""
    api_key = os.environ.get("DAYTONA_API_KEY")
    if not api_key:
        return None

    config = DaytonaConfig(
        api_key=api_key,
        api_url=os.environ.get("DAYTONA_API_URL", "https://app.daytona.io/api"),
        target=os.environ.get("DAYTONA_TARGET", "us"),
    )
    daytona = Daytona(config)
    params = CreateSandboxFromImageParams(
        image="node:20",
        auto_delete_interval=3600,
        public=True,
    )
    return daytona.create(params)
