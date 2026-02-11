import asyncio
import base64
import json
import logging
import os
import re
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

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_HTML_CHARS = 100_000
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
    # shadcn/ui components
    "src/components/ui/button.tsx",
    "src/components/ui/card.tsx",
    "src/components/ui/badge.tsx",
    "src/components/ui/separator.tsx",
    "src/components/ui/input.tsx",
    "src/components/ui/textarea.tsx",
    "src/components/ui/avatar.tsx",
    "src/components/ui/tabs.tsx",
    "src/components/ui/accordion.tsx",
    "src/components/ui/scroll-area.tsx",
    "src/components/ui/dialog.tsx",
    "src/components/ui/dropdown-menu.tsx",
    "src/components/ui/navigation-menu.tsx",
    "src/components/ui/skeleton.tsx",
    "src/components/ui/progress.tsx",
    "src/components/ui/alert.tsx",
]


class CloneRequest(BaseModel):
    url: HttpUrl


VIEWPORT_WIDTH = 1280
VIEWPORT_HEIGHT = 900
MAX_SCREENSHOTS = 8  # cap to avoid massive payloads
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
    # The generated file must start with "use client" or an import statement.
    for marker in ['"use client"', "'use client'", "import "]:
        idx = text.find(marker)
        if idx != -1:
            text = text[idx:]
            break
    return text.strip()


def _extract_shadcn_components(code: str) -> list[str]:
    """Parse generated code to find which shadcn/ui components are actually imported."""
    pattern = r'from\s+["\']@/components/ui/([^"\']+)["\']'
    return list(set(re.findall(pattern, code)))


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
            "max_tokens": 32000,
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


@router.post("/clone")
async def clone_website(req: CloneRequest):
    """Scrape a URL and stream progress via SSE, then return the AI-generated clone."""
    url_str = str(req.url)

    async def event_stream():
        try:
            # ── SCRAPING ──────────────────────────────────────────
            yield _status("scraping", "Scraping website...")
            yield _log(f"Target URL: {url_str}")
            yield _log("Launching headless browser (Chromium)...")
            await asyncio.sleep(0)

            try:
                async with async_playwright() as p:
                    browser = await p.chromium.launch(headless=True)
                    yield _log(f"Browser launched — viewport {VIEWPORT_WIDTH}x{VIEWPORT_HEIGHT}")
                    page = await browser.new_page(
                        viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT}
                    )

                    yield _log(f"Navigating to {url_str}...")
                    await asyncio.sleep(0)
                    await page.goto(url_str, wait_until="networkidle", timeout=30000)
                    yield _log("Page loaded (networkidle)")

                    # First pass: scroll to bottom to trigger all lazy-loaded content
                    yield _log("Scrolling page to trigger lazy-loaded content...")
                    await asyncio.sleep(0)
                    prev_height = 0
                    for _ in range(30):  # safety limit
                        total_height = await page.evaluate("document.body.scrollHeight")
                        if total_height == prev_height:
                            break
                        prev_height = total_height
                        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        await page.wait_for_timeout(800)
                    # Scroll back to top
                    await page.evaluate("window.scrollTo(0, 0)")
                    await page.wait_for_timeout(500)
                    yield _log("Lazy-load scroll complete")

                    # Now capture HTML after all content is loaded
                    html = await page.content()
                    yield _log(f"Extracted HTML — {len(html):,} chars")

                    yield _log("Extracting image URLs...")
                    await asyncio.sleep(0)
                    image_urls: list[str] = await page.evaluate("""() => {
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
                        return [...urls].slice(0, 50);
                    }""")
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
                    yield _log(f"Scraping complete — {len(screenshots)} screenshots captured")
            except Exception as e:
                raise HTTPException(status_code=422, detail=f"Failed to scrape page: {e}")

            # ── GENERATING ────────────────────────────────────────
            yield _status("generating", "Generating clone with AI...")
            yield _log(f"Preparing prompt with {len(screenshots)} screenshots + {len(image_urls)} image URLs...")
            await asyncio.sleep(0)

            api_key = os.environ.get("OPENROUTER_API_KEY")
            if not api_key:
                raise HTTPException(status_code=500, detail="OPENROUTER_API_KEY is not set")

            truncated_html = html[:MAX_HTML_CHARS]
            image_list = "\n".join(f"  - {u}" for u in image_urls) if image_urls else "  (none found)"
            n = len(screenshots)

            prompt = (
                "You are a website cloning expert. Given the HTML source and a series of screenshots capturing "
                f"the ENTIRE page (scrolled top to bottom in {n} viewport-sized chunks), "
                "generate a Next.js page component (page.tsx) that visually replicates the ENTIRE page.\n\n"
                "Tech stack available in the project:\n"
                "- React 19 with Next.js 16 App Router\n"
                "- Tailwind CSS for all styling\n"
                "- shadcn/ui components — import from \"@/components/ui/<name>\"\n"
                "  Available after setup: button, card, badge, avatar, separator, accordion, tabs,\n"
                "  input, textarea, navigation-menu, sheet, dialog, dropdown-menu, popover,\n"
                "  tooltip, select, checkbox, radio-group, switch, slider, progress,\n"
                "  alert, alert-dialog, aspect-ratio, collapsible, scroll-area, skeleton, table, toggle, toggle-group\n"
                "- lucide-react icons — import { IconName } from \"lucide-react\"\n"
                "- Utility: import { cn } from \"@/lib/utils\"\n\n"
                "Rules:\n"
                "- Output ONLY the raw TSX code for page.tsx. No markdown fences, no explanation.\n"
                '- The file MUST start with "use client" and export a default function component.\n'
                "- CRITICAL: The code must be valid TypeScript/JSX with no syntax errors. "
                "Ensure all brackets, braces, and parentheses are properly closed. "
                "Test mentally that the component compiles before outputting.\n"
                "- EXACT TEXT REPRODUCTION: You MUST copy ALL text content EXACTLY as it appears in the HTML source — "
                "company names, brand names, headings, paragraphs, button labels, nav links, footer text, etc. "
                "NEVER substitute, rename, or invent company names or branding. "
                "Read the text from the HTML and use it verbatim.\n"
                "- LOGOS & BRANDING: Reproduce logos exactly. Use the original image URL for logo images. "
                "For inline SVG logos in the HTML, copy the SVG paths exactly — do NOT replace them with generic icons. "
                "The clone must look like the SAME company's website, not a different one.\n"
                "- Use Tailwind CSS utility classes for layout, spacing, colors, typography.\n"
                "- Use shadcn/ui components where they match the original UI (buttons, cards, nav menus, dialogs, badges, etc.).\n"
                "- Use lucide-react for icons that match the original site (for decorative icons only, NOT for logos).\n"
                "- IMAGES: Use the original image URLs with regular <img> tags (NOT next/image). Here are the image URLs extracted:\n"
                f"{image_list}\n"
                "  Use these exact URLs. Match sizing and position from the screenshots.\n"
                "  If an image URL is not listed, check the HTML source for it.\n"
                "  For SVG logos/icons inlined in the HTML, reproduce them as inline SVGs exactly as in the HTML.\n"
                "- FONTS: Use Tailwind font utilities. For specific Google Fonts, add a <link> tag via useEffect or next/head.\n"
                "- You may use React hooks (useState, useEffect, useRef, etc.) for interactivity and animations.\n"
                "- For animations, use Tailwind animate classes (animate-pulse, animate-bounce, etc.) or CSS transitions via className.\n"
                "- IMPORTANT: Reproduce the ENTIRE page from top to bottom, including ALL sections visible across ALL screenshots. "
                f"The {n} screenshots are sequential viewport captures from top to bottom — every section must appear in your output. "
                "The page should scroll naturally just like the original.\n"
                "- Match colors, spacing, font sizes, and layout as closely as possible to the screenshots.\n\n"
                f"Here is the page HTML (may be truncated):\n\n{truncated_html}"
            )

            content: list[dict] = [{"type": "text", "text": prompt}]
            for i, shot_b64 in enumerate(screenshots):
                content.append({"type": "text", "text": f"Screenshot {i + 1} of {n}:"})
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{shot_b64}"},
                })

            yield _log("Sending request to claude-opus-4.5 via OpenRouter...")
            await asyncio.sleep(0)

            ai_messages = [{"role": "user", "content": content}]

            async with httpx.AsyncClient(timeout=120) as client:
                try:
                    raw_response = await _call_ai(client, ai_messages, api_key)
                except httpx.HTTPStatusError as e:
                    raise HTTPException(
                        status_code=502,
                        detail=f"OpenRouter API error: {e.response.status_code} — {e.response.text}",
                    )
                except httpx.RequestError as e:
                    raise HTTPException(
                        status_code=502, detail=f"Failed to reach OpenRouter: {e}"
                    )
                except (KeyError, IndexError):
                    raise HTTPException(status_code=502, detail="Unexpected OpenRouter response format")

            generated_code = strip_markdown_fences(raw_response)
            yield _log(f"AI generated {len(generated_code):,} chars of React code")

            components = _extract_shadcn_components(generated_code)
            if components:
                yield _log(f"shadcn components used: {', '.join(sorted(components))}")
            else:
                yield _log("No shadcn component imports detected")

            # ── DEPLOYING ─────────────────────────────────────────
            yield _status("deploying", "Creating sandbox...")
            yield _log("Creating Daytona sandbox (node:20)...")
            await asyncio.sleep(0)

            preview_url = None
            try:
                sandbox = await asyncio.to_thread(_create_sandbox)
                if sandbox is None:
                    yield _log("DAYTONA_API_KEY not set — skipping deployment")
                else:
                    yield _log("Sandbox created successfully")
                    project_dir = "/home/daytona/app"

                    # Upload files
                    yield _status("deploying", "Uploading project files...")
                    yield _log("Creating project directory structure...")
                    await asyncio.to_thread(
                        sandbox.process.exec,
                        f"mkdir -p {project_dir}/src/app {project_dir}/src/lib {project_dir}/src/components/ui"
                    )

                    for rel_path in TEMPLATE_FILES:
                        yield _log(f"  Uploading {rel_path}")
                        file_path = TEMPLATE_DIR / rel_path
                        await asyncio.to_thread(
                            sandbox.fs.upload_file,
                            file_path.read_bytes(),
                            f"{project_dir}/{rel_path}",
                        )

                    yield _log("  Uploading src/app/page.tsx (generated)")
                    await asyncio.to_thread(
                        sandbox.fs.upload_file,
                        generated_code.encode("utf-8"),
                        f"{project_dir}/src/app/page.tsx",
                    )

                    # npm install
                    yield _status("deploying", "Installing dependencies...")
                    yield _log("Running npm install...")
                    await asyncio.sleep(0)
                    await asyncio.to_thread(
                        sandbox.process.exec,
                        f"cd {project_dir} && npm install",
                    )
                    yield _log("npm install complete")

                    # ── BUILD CHECK WITH RETRY ────────────────────────
                    build_ok = False
                    for attempt in range(1, MAX_BUILD_ATTEMPTS + 1):
                        yield _status("deploying", f"Building project (attempt {attempt}/{MAX_BUILD_ATTEMPTS})...")
                        yield _log(f"Running next build (attempt {attempt}/{MAX_BUILD_ATTEMPTS})...")
                        await asyncio.sleep(0)

                        # Upload latest code before each build attempt
                        if attempt > 1:
                            yield _log("  Re-uploading fixed page.tsx...")
                            await asyncio.to_thread(
                                sandbox.fs.upload_file,
                                generated_code.encode("utf-8"),
                                f"{project_dir}/src/app/page.tsx",
                            )

                        build_result = await asyncio.to_thread(
                            sandbox.process.exec,
                            f"cd {project_dir} && npx next build 2>&1",
                        )

                        build_output = build_result.result or ""
                        if build_result.exit_code == 0:
                            yield _log("Build succeeded!")
                            build_ok = True
                            break

                        # Build failed — truncate error output for the fix prompt
                        error_text = build_output[-3000:] if len(build_output) > 3000 else build_output
                        yield _log(f"Build failed (exit code {build_result.exit_code})")
                        yield _log(f"Error output:\n{error_text[:500]}")

                        if attempt < MAX_BUILD_ATTEMPTS:
                            yield _status("fixing", f"Asking AI to fix code (attempt {attempt + 1}/{MAX_BUILD_ATTEMPTS})...")
                            yield _log(f"Sending build errors to AI for fix (attempt {attempt + 1}/{MAX_BUILD_ATTEMPTS})...")
                            await asyncio.sleep(0)

                            fix_messages = [
                                {"role": "user", "content": content},
                                {"role": "assistant", "content": generated_code},
                                {"role": "user", "content": (
                                    "The code above failed to build with Next.js. Here is the build error output:\n\n"
                                    f"```\n{error_text}\n```\n\n"
                                    "Please fix the code and output ONLY the corrected page.tsx file. "
                                    "No markdown fences, no explanation — just the raw TSX code."
                                )},
                            ]

                            try:
                                async with httpx.AsyncClient(timeout=120) as fix_client:
                                    fix_response = await _call_ai(fix_client, fix_messages, api_key)
                                generated_code = strip_markdown_fences(fix_response)
                                yield _log(f"AI returned fixed code ({len(generated_code):,} chars)")
                            except Exception as fix_err:
                                yield _log(f"AI fix request failed: {fix_err}")
                                break
                        else:
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
                    yield _log(f"Preview ready: {preview_url}")
            except Exception as e:
                logger.warning(f"Daytona sandbox deployment failed: {e}")
                yield _log(f"Deployment error: {e}")

            # ── DONE ──────────────────────────────────────────────
            yield _sse_event({"status": "done", "code": generated_code, "preview_url": preview_url})

        except HTTPException as e:
            yield _log(f"Error: {e.detail}")
            yield _sse_event({"status": "error", "message": e.detail})
        except Exception as e:
            yield _log(f"Error: {e}")
            yield _sse_event({"status": "error", "message": str(e)})

    return StreamingResponse(event_stream(), media_type="text/event-stream")


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
