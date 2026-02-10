import base64
import logging
import os

import httpx
from daytona import Daytona, DaytonaConfig
from fastapi import APIRouter, HTTPException
from playwright.async_api import async_playwright
from pydantic import BaseModel, HttpUrl

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_HTML_CHARS = 50_000


class CloneRequest(BaseModel):
    url: HttpUrl


class CloneResponse(BaseModel):
    html: str
    preview_url: str | None = None


async def scrape_page(url: str) -> tuple[str, str]:
    """Use Playwright to get page HTML and a full-page screenshot (base64)."""
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1280, "height": 900})
            await page.goto(url, wait_until="networkidle", timeout=30000)
            html = await page.content()
            screenshot_bytes = await page.screenshot(full_page=False)
            await browser.close()
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Failed to scrape page: {e}")

    screenshot_b64 = base64.b64encode(screenshot_bytes).decode("utf-8")
    return html, screenshot_b64


async def generate_clone(html: str, screenshot_b64: str) -> str:
    """Send HTML + screenshot to OpenRouter and get back a cloned HTML file."""
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="OPENROUTER_API_KEY is not set")

    truncated_html = html[:MAX_HTML_CHARS]

    prompt = (
        "You are a website cloning expert. Given the HTML source and a screenshot of a webpage, "
        "generate a single self-contained HTML file that visually replicates the page as closely as possible.\n\n"
        "Rules:\n"
        "- Output ONLY the raw HTML. No markdown fences, no explanation.\n"
        "- All CSS must be inlined in a <style> tag.\n"
        "- All JS must be inlined in a <script> tag.\n"
        "- Do NOT reference any external stylesheets, scripts, or fonts.\n"
        "- Replace images with colored placeholder divs that match the approximate size and position.\n"
        "- The page should look as close as possible to the screenshot.\n\n"
        f"Here is the page HTML (may be truncated):\n\n{truncated_html}"
    )

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{screenshot_b64}",
                    },
                },
            ],
        }
    ]

    async with httpx.AsyncClient(timeout=120) as client:
        try:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "anthropic/claude-sonnet-4",
                    "messages": messages,
                    "max_tokens": 16000,
                },
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise HTTPException(
                status_code=502,
                detail=f"OpenRouter API error: {e.response.status_code} — {e.response.text}",
            )
        except httpx.RequestError as e:
            raise HTTPException(
                status_code=502, detail=f"Failed to reach OpenRouter: {e}"
            )

    data = resp.json()
    try:
        generated_html = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        raise HTTPException(status_code=502, detail="Unexpected OpenRouter response format")

    return generated_html


def deploy_to_sandbox(html: str) -> str | None:
    """Deploy generated HTML to a Daytona sandbox and return the preview URL."""
    api_key = os.environ.get("DAYTONA_API_KEY")
    if not api_key:
        logger.info("DAYTONA_API_KEY not set, skipping sandbox deployment")
        return None

    config = DaytonaConfig(
        api_key=api_key,
        api_url=os.environ.get("DAYTONA_API_URL", "https://app.daytona.io/api"),
        target=os.environ.get("DAYTONA_TARGET", "us"),
    )
    daytona = Daytona(config)
    sandbox = daytona.create(auto_delete_interval=3600)

    sandbox.fs.upload_file(html.encode("utf-8"), "/home/daytona/index.html")
    sandbox.process.exec("cd /home/daytona && python3 -m http.server 8080 &")

    preview = sandbox.get_preview_link(8080)
    return preview.url


@router.post("/clone", response_model=CloneResponse)
async def clone_website(req: CloneRequest):
    """Scrape a URL and return an AI-generated HTML clone."""
    url_str = str(req.url)
    html, screenshot_b64 = await scrape_page(url_str)
    generated_html = await generate_clone(html, screenshot_b64)

    preview_url = None
    try:
        preview_url = deploy_to_sandbox(generated_html)
    except Exception as e:
        logger.warning(f"Daytona sandbox deployment failed: {e}")

    return CloneResponse(html=generated_html, preview_url=preview_url)
