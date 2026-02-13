import asyncio
import base64
import logging
import re
import time
from typing import Any, Callable

from playwright.async_api import async_playwright
from playwright_stealth import Stealth

logger = logging.getLogger(__name__)

VIEWPORT_WIDTH = 1280
VIEWPORT_HEIGHT = 900
MAX_SCREENSHOTS = 15
MAX_IMAGE_URLS = 100
MAX_STRUCTURED_ELEMENTS = 300
STEP_TIMEOUT = 10  # Max seconds per JS evaluation step


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
        const seen = new Set();

        // Find top-level nav items: direct links/buttons in the nav
        const topCandidates = nav.querySelectorAll(
            ':scope > ul > li, :scope > div > ul > li, ' +
            ':scope > div > div > a, :scope > div > div > button, ' +
            'li, [role="menuitem"]'
        );

        for (const li of topCandidates) {
            const link = li.querySelector(':scope > a, :scope > button') || li.closest('a') || li;
            // Get only the direct text, not nested dropdown text
            const directText = (link.childNodes.length > 0
                ? [...link.childNodes].filter(n => n.nodeType === 3 || (n.nodeType === 1 && !n.querySelector('ul, [role="menu"], div[class*="dropdown"], div[class*="popover"], div[class*="panel"], div[class*="mega"]')))
                    .map(n => n.textContent?.trim()).filter(Boolean).join(' ')
                : link.textContent?.trim()) || '';
            const text = directText.substring(0, 100);
            if (!text || seen.has(text)) continue;
            seen.add(text);

            const menuItem = { label: text };

            // Check if this item has a dropdown/mega-menu panel
            // Look for: nested ul, aria-controlled panel, adjacent dropdown div, hidden sibling panels
            let dropdownPanel = li.querySelector(
                'ul, [role="menu"], div[class*="dropdown"], div[class*="popover"], ' +
                'div[class*="panel"], div[class*="mega"], div[class*="submenu"], div[class*="flyout"]'
            );
            // Also check aria-controls for panel ID
            const trigger = li.querySelector('[aria-controls], [aria-expanded]') || li;
            if (!dropdownPanel) {
                const controlsId = trigger.getAttribute('aria-controls');
                if (controlsId) {
                    dropdownPanel = document.getElementById(controlsId);
                }
            }
            // Check next sibling (some sites place dropdown as adjacent element)
            if (!dropdownPanel && li.nextElementSibling) {
                const sib = li.nextElementSibling;
                if (sib.matches && sib.matches('div[class*="dropdown"], div[class*="popover"], div[class*="panel"], div[class*="mega"], [role="menu"]')) {
                    dropdownPanel = sib;
                }
            }

            if (dropdownPanel) {
                menuItem.dropdown = [];

                // Try to find grouped sections within the dropdown (mega-menu columns)
                const sections = dropdownPanel.querySelectorAll(
                    ':scope > div > div, :scope > div > ul, [class*="group"], [class*="column"], [class*="section"]'
                );
                const useGroups = sections.length >= 2;

                if (useGroups) {
                    // Mega-menu with groups/columns
                    menuItem.dropdownLayout = 'mega';
                    const groupSeen = new Set();
                    for (const section of sections) {
                        if (menuItem.dropdown.length >= 30) break;
                        // Find group heading
                        const groupHeading = section.querySelector('h2, h3, h4, h5, h6, [class*="heading"], [class*="title"], span[class*="label"]');
                        const groupTitle = groupHeading?.textContent?.trim().substring(0, 80);
                        if (groupTitle && groupSeen.has(groupTitle)) continue;
                        if (groupTitle) groupSeen.add(groupTitle);

                        const groupItems = [];
                        const links = section.querySelectorAll('a, button[role="menuitem"]');
                        const subSeen = new Set();
                        for (const a of links) {
                            if (groupItems.length >= 10) break;
                            // Extract the item's own heading text (first strong/heading child or direct text)
                            const itemHeading = a.querySelector('h3, h4, h5, h6, strong, span[class*="title"], span[class*="name"], div[class*="title"]');
                            const itemTitle = (itemHeading?.textContent?.trim() || a.textContent?.trim() || '').substring(0, 100);
                            if (!itemTitle || subSeen.has(itemTitle) || itemTitle === text) continue;
                            subSeen.add(itemTitle);

                            const subItem = { title: itemTitle };
                            // Extract description
                            const desc = a.querySelector('p, span[class*="desc"], span[class*="subtitle"], div[class*="desc"]');
                            if (desc) subItem.description = desc.textContent?.trim().substring(0, 150);
                            // Extract icon/SVG
                            const svg = a.querySelector('svg');
                            if (svg) {
                                subItem.svgMarkup = svg.outerHTML.substring(0, 1500);
                                const vb = svg.getAttribute('viewBox');
                                if (vb) subItem.svgViewBox = vb;
                            }
                            const icon = a.querySelector('img');
                            if (icon) subItem.iconSrc = icon.src;

                            groupItems.push(subItem);
                        }
                        if (groupItems.length > 0) {
                            menuItem.dropdown.push({
                                groupTitle: groupTitle || undefined,
                                items: groupItems,
                            });
                        }
                    }
                } else {
                    // Simple flat dropdown
                    menuItem.dropdownLayout = 'list';
                    const links = dropdownPanel.querySelectorAll('a, button[role="menuitem"], [role="menuitem"]');
                    const subSeen = new Set();
                    for (const a of links) {
                        if (menuItem.dropdown.length >= 20) break;
                        const itemHeading = a.querySelector('h3, h4, h5, h6, strong, span[class*="title"], span[class*="name"]');
                        const itemTitle = (itemHeading?.textContent?.trim() || a.textContent?.trim() || '').substring(0, 100);
                        if (!itemTitle || subSeen.has(itemTitle) || itemTitle === text) continue;
                        subSeen.add(itemTitle);

                        const subItem = { title: itemTitle };
                        const desc = a.querySelector('p, span[class*="desc"], span[class*="subtitle"]');
                        if (desc) subItem.description = desc.textContent?.trim().substring(0, 150);
                        const svg = a.querySelector('svg');
                        if (svg) {
                            subItem.svgMarkup = svg.outerHTML.substring(0, 1500);
                        }
                        const icon = a.querySelector('img');
                        if (icon) subItem.iconSrc = icon.src;

                        menuItem.dropdown.push(subItem);
                    }
                }
                if (menuItem.dropdown.length === 0) {
                    delete menuItem.dropdown;
                } else {
                    // Extract dropdown panel styling
                    const ps = getComputedStyle(dropdownPanel);
                    const pr = dropdownPanel.getBoundingClientRect();
                    menuItem.panelStyle = {
                        backgroundColor: ps.backgroundColor,
                        border: ps.border,
                        borderRadius: ps.borderRadius,
                        boxShadow: ps.boxShadow !== 'none' ? ps.boxShadow : undefined,
                        padding: ps.padding,
                        width: Math.round(pr.width),
                        minWidth: ps.minWidth,
                        position: ps.position,
                    };
                }
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

    // Broad carousel/slider selectors — catches most carousel implementations
    const carouselSelectors = [
        '[class*="carousel"]', '[class*="slider"]', '[class*="swiper"]',
        '[class*="slide"]', '[data-carousel]', '[data-slider]',
        '[role="tabpanel"]', '[class*="testimonial"]',
        '[class*="card-stack"]', '[class*="rotating"]',
        '[class*="scroll"]', '[class*="horizontal"]', '[class*="marquee"]',
        '[class*="track"]', '[class*="strip"]', '[class*="ticker"]'
    ];

    const containers = document.querySelectorAll(carouselSelectors.join(', '));
    const seen = new Set();

    for (const container of containers) {
        // Skip if this is a child of an already-processed carousel
        if (seen.has(container) || [...seen].some(s => s.contains(container))) continue;

        // Find all slide-like children
        const slideSelectors = [
            ':scope > div', ':scope > li', ':scope > article',
            '[class*="slide"]', '[role="tabpanel"]', '[class*="item"]',
            ':scope > a'
        ];
        let slides = [];
        for (const sel of slideSelectors) {
            const found = container.querySelectorAll(sel);
            if (found.length > 1) { slides = [...found]; break; }
        }
        if (slides.length < 2) continue;

        seen.add(container);

        // ── Container + card dimensions ──
        const containerStyle = getComputedStyle(container);
        const containerRect = container.getBoundingClientRect();
        const containerWidth = Math.round(containerRect.width);
        const containerHeight = Math.round(containerRect.height);
        const gap = parseFloat(containerStyle.gap) || 0;
        const firstSlideRect = slides[0].getBoundingClientRect();
        const cardWidth = Math.round(firstSlideRect.width);
        const cardHeight = Math.round(firstSlideRect.height);
        const visibleCards = cardWidth > 0 ? Math.round((containerWidth + gap) / (cardWidth + gap)) : 1;

        // ── Infinite scroll detection ──
        // Check for: CSS animations, transform on container/parent, duplicated content, overflow hidden
        const parentEl = container.parentElement;
        const parentStyle = parentEl ? getComputedStyle(parentEl) : {};
        const hasAnimation = containerStyle.animation !== 'none' && containerStyle.animation !== '';
        const hasTransform = containerStyle.transform !== 'none' && containerStyle.transform !== '';
        const parentHasOverflowHidden = parentStyle.overflow === 'hidden' || parentStyle.overflowX === 'hidden';
        const containerHasOverflowHidden = containerStyle.overflow === 'hidden' || containerStyle.overflowX === 'hidden';

        // Detect duplicate slides (infinite carousels clone DOM nodes)
        const slideTexts = slides.map(s => s.textContent?.trim().substring(0, 100) || '');
        const uniqueTexts = new Set(slideTexts.filter(t => t.length > 0));
        const hasDuplicates = uniqueTexts.size > 0 && uniqueTexts.size < slideTexts.filter(t => t.length > 0).length * 0.7;
        const isInfinite = hasDuplicates || hasAnimation || (
            hasTransform && (parentHasOverflowHidden || containerHasOverflowHidden)
        );

        const scrollBehavior = {
            transform: hasTransform ? containerStyle.transform : undefined,
            animation: hasAnimation ? containerStyle.animation : undefined,
            transition: (containerStyle.transition && containerStyle.transition !== 'all 0s ease 0s') ? containerStyle.transition : undefined,
            overflowX: containerStyle.overflowX,
            parentOverflowX: parentStyle.overflowX || undefined,
            display: containerStyle.display,
        };

        const carousel = {
            type: container.className.includes('tab') ? 'tabs' : 'carousel',
            selector: container.className.split(' ').filter(c => c.length > 2).slice(0, 3).join('.'),
            totalDomSlides: slides.length,
            containerWidth,
            containerHeight,
            gap,
            overflow: containerStyle.overflow,
            containerDisplay: containerStyle.display,
            cardWidth,
            cardHeight,
            visibleCards,
            isInfinite,
            scrollBehavior,
            slides: []
        };

        // ── Extract unique slides (deduplicate cloned nodes for infinite carousels) ──
        const seenContent = new Set();
        for (const slide of slides) {
            if (carousel.slides.length >= 20) break;
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

            // Deduplicate: skip slides with identical content (clones for infinite scroll)
            const contentKey = (slideData.title || '') + '|' + (slideData.description || '') + '|' + (slideData.text || '') + '|' + (slideData.image || '');
            if (contentKey.length > 1 && seenContent.has(contentKey)) continue;
            if (contentKey.length > 1) seenContent.add(contentKey);

            // ── SVG extraction — capture ALL SVGs inside each slide ──
            const svgs = slide.querySelectorAll('svg');
            if (svgs.length > 0) {
                slideData.svgCount = svgs.length;
                slideData.svgMarkups = [];
                for (const svg of [...svgs].slice(0, 3)) {
                    const markup = svg.outerHTML;
                    slideData.svgMarkups.push(markup.substring(0, 2000));
                    if (!slideData.svgViewBox) {
                        const vb = svg.getAttribute('viewBox');
                        if (vb) slideData.svgViewBox = vb;
                    }
                }
            }

            // ── Icon extraction (non-SVG: img icons, emoji, icon fonts) ──
            const icons = slide.querySelectorAll('img[src*="icon"], img[width][height], i[class*="icon"], span[class*="icon"]');
            if (icons.length > 0) {
                slideData.icons = [];
                for (const icon of [...icons].slice(0, 3)) {
                    if (icon.tagName === 'IMG') {
                        slideData.icons.push({ type: 'img', src: icon.src, alt: icon.alt || '' });
                    } else {
                        slideData.icons.push({ type: 'class', className: icon.className.substring(0, 100) });
                    }
                }
            }

            // ── Card styling per slide ──
            const slideStyle = getComputedStyle(slide);
            slideData.cardStyle = {
                backgroundColor: slideStyle.backgroundColor,
                border: slideStyle.border,
                borderRadius: slideStyle.borderRadius,
                boxShadow: slideStyle.boxShadow !== 'none' ? slideStyle.boxShadow : undefined,
                padding: slideStyle.padding,
            };

            if (Object.keys(slideData).length > 0) carousel.slides.push(slideData);
        }

        carousel.slideCount = carousel.slides.length; // unique slide count
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

# JavaScript to extract font URLs from CSS (Google Fonts links, @font-face rules, @import)
_JS_EXTRACT_FONTS = """() => {
    const googleFontLinks = [];
    const fontFaceRules = [];

    // 1. Scan <link> tags for Google Fonts / Adobe Fonts
    document.querySelectorAll('link[href]').forEach(link => {
        const href = link.href || '';
        if (href.includes('fonts.googleapis.com') || href.includes('fonts.gstatic.com') || href.includes('use.typekit.net')) {
            googleFontLinks.push(href);
        }
    });

    // 2. Scan <style> tags for @import url(...) pointing to font providers
    document.querySelectorAll('style').forEach(style => {
        const text = style.textContent || '';
        const importRegex = /@import\\s+url\\(["']?([^"')]+)["']?\\)/g;
        let m;
        while ((m = importRegex.exec(text)) !== null) {
            const url = m[1];
            if (url.includes('fonts.googleapis.com') || url.includes('use.typekit.net')) {
                googleFontLinks.push(url);
            }
        }
    });

    // 3. Scan all styleSheets for @font-face rules
    for (const sheet of document.styleSheets) {
        try {
            for (const rule of sheet.cssRules) {
                if (rule instanceof CSSFontFaceRule) {
                    const family = rule.style.getPropertyValue('font-family').replace(/['"]/g, '').trim();
                    const src = rule.style.getPropertyValue('src');
                    const weight = rule.style.getPropertyValue('font-weight') || '400';
                    const style = rule.style.getPropertyValue('font-style') || 'normal';
                    if (family && src) {
                        fontFaceRules.push({ family, src: src.substring(0, 500), weight, style });
                    }
                }
                if (rule instanceof CSSImportRule && rule.href) {
                    if (rule.href.includes('fonts.googleapis.com') || rule.href.includes('use.typekit.net')) {
                        googleFontLinks.push(rule.href);
                    }
                }
            }
        } catch(e) {} // cross-origin sheets
    }

    return { googleFontLinks: [...new Set(googleFontLinks)], fontFaceRules: fontFaceRules.slice(0, 20) };
}"""

# JavaScript to extract image URLs with context
_JS_EXTRACT_IMAGES = """(maxUrls) => {
    const images = [];
    const seen = new Set();
    const add = (url, meta) => {
        if (!url || seen.has(url)) return;
        seen.add(url);
        images.push({ url, ...meta });
    };
    // img elements — with alt, dimensions, container context
    document.querySelectorAll('img[src]').forEach(img => {
        if (!img.src) return;
        const rect = img.getBoundingClientRect();
        const parent = img.closest('section, article, div[class], header, footer, nav');
        const nearby = img.closest('a, div, figure');
        const nearbyText = nearby ? nearby.textContent?.trim().substring(0, 60) : '';
        add(img.src, {
            alt: img.alt || '',
            width: Math.round(rect.width) || undefined,
            height: Math.round(rect.height) || undefined,
            container: parent?.className?.split(' ').filter(c => c.length > 2).slice(0, 2).join('.') || '',
            context: nearbyText || '',
        });
    });
    // srcset entries
    document.querySelectorAll('img[srcset], source[srcset]').forEach(el => {
        el.srcset.split(',').forEach(s => {
            const u = s.trim().split(/\\s+/)[0];
            try { const abs = new URL(u, location.href).href; add(abs, {}); } catch(e) {}
        });
    });
    // background-image URLs (scan key containers only, not every DOM node)
    document.querySelectorAll('section, article, div[class*="hero"], div[class*="banner"], div[class*="bg"], div[class*="background"], div[class*="cover"], header, footer, [style*="background"]').forEach(el => {
        const bg = getComputedStyle(el).backgroundImage;
        const match = bg.match(/url\\(["']?([^"')]+)["']?\\)/);
        if (match && match[1]) {
            try { const abs = new URL(match[1], location.href).href; add(abs, { context: 'background-image' }); } catch(e) {}
        }
    });
    // Favicon / icon links
    document.querySelectorAll('link[rel*="icon"][href]').forEach(link => {
        if (link.href) add(link.href, { context: 'favicon' });
    });
    return images.slice(0, maxUrls);
}"""


# Type alias for the callback functions
LogCallback = Callable[[str], str]
StatusCallback = Callable[[str, str], str]


async def _safe_evaluate(page, script, arg=None, timeout=STEP_TIMEOUT, default=None):
    """Run page.evaluate with a hard timeout. Returns default on timeout/error."""
    try:
        coro = page.evaluate(script, arg) if arg is not None else page.evaluate(script)
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("[scrape] JS evaluation timed out after %ds", timeout)
        return default
    except Exception as e:
        logger.warning("[scrape] JS evaluation failed: %s", e)
        return default


async def scrape_page(
    url: str,
    on_log: LogCallback,
    on_status: StatusCallback,
    log_queue: list[str] | None = None,
) -> dict[str, Any]:
    """Scrape a URL using Playwright and return all extracted data.

    Args:
        url: The URL to scrape.
        on_log: Callback that formats a log message as an SSE event string.
        on_status: Callback that formats a status event string.
        log_queue: Optional shared list; scraper appends progress messages here
                   so the SSE generator can stream them in real time.

    Returns:
        Dict with keys: raw_html, html, computed_styles, structured_content,
        nav_structure, interactive_elements, font_data, image_urls,
        screenshots, scroll_positions, total_height.
    """
    def _log(msg: str) -> None:
        """Append a progress message for the frontend terminal."""
        logger.info("[scrape] %s", msg)
        if log_queue is not None:
            log_queue.append(msg)

    result: dict[str, Any] = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        _log(f"Browser launched for {url}")
        page = await browser.new_page(
            viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        await Stealth().apply_stealth_async(page)

        nav_start = time.time()
        _log("Navigating to page...")

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            _log(f"Navigation timeout ({e.__class__.__name__}), continuing with partial page...")

        # Brief settle time for JS hydration / async renders
        await page.wait_for_timeout(2000)
        _log(f"Page loaded in {time.time() - nav_start:.1f}s")

        # Dismiss cookie consent / overlay banners
        _log("Dismissing overlays...")
        dismissed = await _safe_evaluate(page, """() => {
            const selectors = [
                'button[id*="accept"]', 'button[class*="accept"]',
                'button[id*="consent"]', 'button[class*="consent"]',
                'button[id*="agree"]', 'button[class*="agree"]',
                '[data-testid*="accept"]', '[data-testid*="consent"]',
                'button[aria-label*="Accept"]', 'button[aria-label*="accept"]',
                'button[aria-label*="Allow"]', 'button[aria-label*="allow"]',
                '[class*="cookie"] button', '[id*="cookie"] button',
                '[class*="banner"] button:first-of-type',
                'button[aria-label*="Accept the use"]',
                'tp-yt-paper-button[aria-label*="Agree"]',
                'button.VfPpkd-LgbsSe[style*="background"]',
                '#sp-cc-accept', 'input[name="accept"]',
            ];
            let count = 0;
            for (const sel of selectors) {
                const el = document.querySelector(sel);
                if (el && el.offsetParent !== null) { el.click(); count++; }
            }
            return count;
        }""", default=0)
        if dismissed:
            await page.wait_for_timeout(1000)
            _log(f"Dismissed {dismissed} overlay(s)")

        # Scroll to bottom to trigger lazy-loaded content
        _log("Scrolling to trigger lazy-loaded content...")
        scroll_start = time.time()
        scroll_count = 0
        prev_height = 0
        for _ in range(30):
            total_height = await _safe_evaluate(page, "document.body.scrollHeight", default=0, timeout=5)
            if total_height == 0 or total_height == prev_height:
                break
            prev_height = total_height
            await _safe_evaluate(page, "window.scrollTo(0, document.body.scrollHeight)", default=None, timeout=5)
            await page.wait_for_timeout(800)
            scroll_count += 1
        # Scroll back to top
        await _safe_evaluate(page, "window.scrollTo(0, 0)", default=None, timeout=5)
        await page.wait_for_timeout(500)
        final_height = await _safe_evaluate(page, "document.body.scrollHeight", default=prev_height, timeout=5)
        _log(f"Scrolled {scroll_count}x in {time.time() - scroll_start:.1f}s — page height: {final_height:,}px")

        # Capture HTML after all content is loaded
        _log("Extracting HTML...")
        try:
            raw_html = await asyncio.wait_for(page.content(), timeout=STEP_TIMEOUT)
        except asyncio.TimeoutError:
            _log("HTML extraction timed out, using empty HTML")
            raw_html = ""

        # Clean HTML
        clean_start = time.time()
        html = _clean_html(raw_html)
        reduction = 100 - len(html) * 100 // max(len(raw_html), 1)
        _log(f"HTML cleaned: {len(raw_html):,} → {len(html):,} chars ({reduction}% reduction)")

        # Extract computed styles
        _log("Extracting computed styles...")
        computed_styles: dict = await _safe_evaluate(page, _JS_EXTRACT_STYLES, default={})
        _log(f"Styles: {len(computed_styles.get('fonts', []))} fonts, {len(computed_styles.get('cssVariables', {}))} CSS vars")

        # Extract structured content
        _log("Extracting page content structure...")
        structured_content: list[dict] = await _safe_evaluate(
            page, _JS_EXTRACT_CONTENT, arg=MAX_STRUCTURED_ELEMENTS, default=[]
        )
        _log(f"Found {len(structured_content)} content elements")

        # Trigger navigation dropdowns
        _log("Probing navigation dropdowns...")
        try:
            await asyncio.wait_for(page.evaluate("window.scrollTo(0, 0)"), timeout=5)
            await page.wait_for_timeout(300)

            nav_triggers = await asyncio.wait_for(
                page.query_selector_all(
                    'nav a, nav button, header a, header button, '
                    '[role="navigation"] a, [role="navigation"] button, '
                    '[role="menuitem"], [aria-haspopup="true"], [aria-expanded]'
                ),
                timeout=5,
            )
            triggered_count = 0
            for trigger in nav_triggers[:20]:
                try:
                    is_visible = await trigger.is_visible()
                    if not is_visible:
                        continue
                    box = await trigger.bounding_box()
                    if not box or box["y"] > VIEWPORT_HEIGHT:
                        continue

                    await trigger.hover(timeout=1000)
                    await page.wait_for_timeout(200)

                    has_popup = await trigger.evaluate(
                        "el => el.hasAttribute('aria-haspopup') || el.hasAttribute('aria-expanded') || el.tagName === 'BUTTON'"
                    )
                    if has_popup:
                        await trigger.click(timeout=1000)
                        await page.wait_for_timeout(200)

                    triggered_count += 1
                except Exception:
                    continue

            _log(f"Triggered {triggered_count} nav items for dropdown extraction")
        except (asyncio.TimeoutError, Exception) as nav_err:
            _log("Nav probing timed out, skipping")
            logger.warning("[scrape] Nav trigger failed (non-fatal): %s", nav_err)

        # Extract navigation structure
        nav_structure: list[dict] = await _safe_evaluate(page, _JS_EXTRACT_NAV, default=[])
        total_dropdown_items = sum(
            len(item.get("dropdown", []))
            for nav in nav_structure
            for item in nav.get("items", [])
        )
        _log(f"Navigation: {len(nav_structure)} nav(s), {total_dropdown_items} dropdown items")

        # Close open dropdowns and reset page state
        try:
            await asyncio.wait_for(page.evaluate("document.body.click()"), timeout=3)
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(300)
            await page.evaluate("window.scrollTo(0, 0)")
            await page.wait_for_timeout(200)
        except Exception:
            pass

        # Extract interactive elements
        _log("Extracting interactive elements...")
        interactive_elements: list[dict] = await _safe_evaluate(page, _JS_EXTRACT_INTERACTIVE, default=[])
        total_slides = sum(el.get("slideCount", 0) for el in interactive_elements)
        _log(f"Interactive: {len(interactive_elements)} groups, {total_slides} slides")

        # Extract font URLs
        font_data: dict = await _safe_evaluate(page, _JS_EXTRACT_FONTS, default={"googleFontLinks": [], "fontFaceRules": []})
        google_font_count = len(font_data.get("googleFontLinks", []))
        font_face_count = len(font_data.get("fontFaceRules", []))
        _log(f"Fonts: {google_font_count} Google Font links, {font_face_count} @font-face rules")

        # Extract image URLs
        image_urls: list[dict] = await _safe_evaluate(page, _JS_EXTRACT_IMAGES, arg=MAX_IMAGE_URLS, default=[])
        _log(f"Found {len(image_urls)} image URLs")

        # Take screenshots
        _log("Capturing screenshots...")
        total_height = await _safe_evaluate(page, "document.body.scrollHeight", default=VIEWPORT_HEIGHT)
        screenshots: list[str] = []
        scroll_positions: list[int] = []
        scroll_offset = 0
        while scroll_offset < total_height and len(screenshots) < MAX_SCREENSHOTS:
            try:
                await asyncio.wait_for(page.evaluate(f"window.scrollTo(0, {scroll_offset})"), timeout=5)
                await page.wait_for_timeout(600)
                shot = await asyncio.wait_for(page.screenshot(full_page=False), timeout=10)
                screenshots.append(base64.b64encode(shot).decode("utf-8"))
                scroll_positions.append(scroll_offset)
            except (asyncio.TimeoutError, Exception) as e:
                _log(f"Screenshot at offset {scroll_offset} failed, stopping")
                break
            scroll_offset += VIEWPORT_HEIGHT

        await browser.close()
        screenshot_bytes = sum(len(s) for s in screenshots)
        _log(f"Captured {len(screenshots)} screenshots ({screenshot_bytes / 1_048_576:.1f}MB), page height={total_height}px")

    return {
        "raw_html": raw_html,
        "html": html,
        "computed_styles": computed_styles,
        "structured_content": structured_content,
        "nav_structure": nav_structure,
        "interactive_elements": interactive_elements,
        "font_data": font_data,
        "image_urls": image_urls,
        "screenshots": screenshots,
        "scroll_positions": scroll_positions,
        "total_height": total_height,
    }
