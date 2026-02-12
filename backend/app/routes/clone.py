import asyncio
import json
import logging
import os
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import certifi

# Fix SSL cert verification on macOS (Python can't find system certs)
os.environ.setdefault("SSL_CERT_FILE", certifi.where())
os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, HttpUrl

from app.database import get_supabase
from app.services.scraper import scrape_page, VIEWPORT_WIDTH, VIEWPORT_HEIGHT
from app.services.ai import generate_clone, fix_build_errors
from app.services.deployer import (
    deploy_to_sandbox,
    build_with_retry,
    start_preview,
    capture_static_html,
    _create_sandbox,
    TEMPLATE_DIR,
    TEMPLATE_FILES,
    MAX_BUILD_ATTEMPTS,
)

logger = logging.getLogger(__name__)

router = APIRouter()


class CloneRequest(BaseModel):
    url: HttpUrl


# ── SSE helpers ──────────────────────────────────────────────


def _sse_event(data: dict) -> str:
    """Format a dict as an SSE data line."""
    return f"data: {json.dumps(data)}\n\n"


def _log(msg: str) -> str:
    """Shorthand: emit an SSE log event."""
    return _sse_event({"log": msg})


def _status(status: str, message: str) -> str:
    """Shorthand: emit an SSE status event."""
    return _sse_event({"status": status, "message": message})


# ── DB helpers ───────────────────────────────────────────────


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


# ── Route handlers ───────────────────────────────────────────


@router.post("/clone")
async def clone_website(req: CloneRequest):
    """Scrape a URL and stream progress via SSE, then return the AI-generated clone."""
    url_str = str(req.url)
    request_start = time.time()
    logger.info("=== CLONE REQUEST START === url=%s", url_str)
    clone_id = _db_insert_clone(url_str)

    async def event_stream():
        try:
            # ── SCRAPING + SANDBOX CREATION (parallel) ────────────
            scrape_start = time.time()
            yield _status("scraping", "Scraping website...")
            yield _log(f"Target URL: {url_str}")
            yield _log("Launching headless browser + pre-creating sandbox...")
            await asyncio.sleep(0)

            # Start sandbox creation in background while scraping runs
            sandbox_task = asyncio.create_task(asyncio.to_thread(_create_sandbox))

            # Shared log queue — scraper appends messages, we drain as SSE
            scrape_logs: list[str] = []

            try:
                scrape_task = asyncio.create_task(
                    scrape_page(url_str, on_log=_log, on_status=_status, log_queue=scrape_logs)
                )

                # Poll for scraper progress until done
                while not scrape_task.done():
                    await asyncio.sleep(0.3)
                    while scrape_logs:
                        yield _log(scrape_logs.pop(0))

                scrape_data = scrape_task.result()

                # Drain remaining logs
                while scrape_logs:
                    yield _log(scrape_logs.pop(0))

                scrape_elapsed = time.time() - scrape_start
                screenshots = scrape_data["screenshots"]
                image_urls = scrape_data["image_urls"]
                raw_html = scrape_data["raw_html"]
                html = scrape_data["html"]
                computed_styles = scrape_data["computed_styles"]

                yield _log(f"Scraping complete in {scrape_elapsed:.1f}s — {len(screenshots)} screenshots, {len(image_urls)} images")
                _db_update_clone(clone_id, status="generating", screenshot_count=len(screenshots), image_count=len(image_urls), html_raw_size=len(raw_html), html_cleaned_size=len(html))
            except Exception as e:
                logger.error("[scrape] FAILED for %s: %s\n%s", url_str, e, traceback.format_exc())
                sandbox_task.cancel()
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
                sandbox_task.cancel()
                raise HTTPException(status_code=500, detail="OPENROUTER_API_KEY is not set")

            yield _log("Sending request to claude-sonnet-4.5 via OpenRouter...")
            await asyncio.sleep(0)

            # Queue for bridging AI on_status callbacks to SSE events
            ai_status_queue: asyncio.Queue = asyncio.Queue()

            async def on_ai_status(payload: dict):
                await ai_status_queue.put(payload)

            # Extract fields the new AI interface expects
            font_data = scrape_data.get("font_data", {}) or {}
            font_links = font_data.get("googleFontLinks", [])
            scroll_positions = scrape_data.get("scroll_positions", [])
            total_height = scrape_data.get("total_height", 0)
            interactive_elements = scrape_data.get("interactive_elements", [])
            nav_structure = scrape_data.get("nav_structure", [])

            try:
                gen_task = asyncio.create_task(generate_clone(
                    html=html,
                    screenshots=screenshots,
                    image_urls=image_urls,
                    url=url_str,
                    styles=computed_styles,
                    font_links=font_links,
                    interactives=interactive_elements,
                    nav_structure=nav_structure,
                    scroll_positions=scroll_positions,
                    total_height=total_height,
                    on_status=on_ai_status,
                ))

                # Poll for AI status updates and yield as SSE events
                while not gen_task.done():
                    try:
                        payload = await asyncio.wait_for(ai_status_queue.get(), timeout=0.5)
                        if payload.get("type") == "file_write":
                            yield _log(f"+ {payload['file']} ({payload.get('lines', '?')} lines)")
                        elif payload.get("status"):
                            yield _status(payload["status"], payload["message"])
                    except asyncio.TimeoutError:
                        pass

                # Drain remaining status events
                while not ai_status_queue.empty():
                    payload = ai_status_queue.get_nowait()
                    if payload.get("type") == "file_write":
                        yield _log(f"+ {payload['file']} ({payload.get('lines', '?')} lines)")
                    elif payload.get("status"):
                        yield _status(payload["status"], payload["message"])

                result = gen_task.result()
            except Exception as e:
                logger.error("[generate] AI call failed: %s", e)
                sandbox_task.cancel()
                raise HTTPException(status_code=502, detail=f"AI generation failed: {e}")

            # Convert new list format to dict with src/ prefix for deployer
            generated_files: dict[str, str] = {}
            for f in result.get("files", []):
                path = f["path"]
                # AI generates paths like "app/page.tsx", "components/Navbar.tsx"
                # Deployer expects "src/app/page.tsx", "src/components/Navbar.tsx"
                if not path.startswith("src/"):
                    path = f"src/{path}"
                generated_files[path] = f["content"]
            extra_deps = result.get("deps", [])

            # Find page.tsx content — try exact key, then any page.tsx variant
            generated_code = generated_files.get("src/app/page.tsx", "")
            if not generated_code:
                for k, v in generated_files.items():
                    if k.endswith("page.tsx"):
                        generated_code = v
                        break
            yield _log(f"AI generated {len(generated_files)} file(s): {', '.join(generated_files.keys())}")
            if extra_deps:
                yield _log(f"AI requested extra packages: {', '.join(extra_deps)}")

            usage = result.get("usage", {})
            if usage.get("total_cost"):
                yield _log(f"AI cost: ${usage['total_cost']:.4f} ({usage.get('api_calls', 1)} API call(s), {usage.get('agents', 1)} agent(s))")

            gen_elapsed = time.time() - gen_start
            logger.info("[generate] COMPLETE in %.1fs", gen_elapsed)
            _db_update_clone(clone_id, status="deploying", generated_code=generated_code)

            # ── DEPLOYING ─────────────────────────────────────────
            deploy_start = time.time()
            yield _status("deploying", "Preparing sandbox...")
            await asyncio.sleep(0)

            # Queue for streaming deploy progress to SSE
            deploy_queue: asyncio.Queue = asyncio.Queue()

            async def deploy_log(msg: str):
                await deploy_queue.put(msg)

            preview_url = None
            static_html = None
            deploy_error = None

            async def _run_deploy():
                nonlocal preview_url, static_html, generated_files, extra_deps, deploy_error
                try:
                    # Await the pre-created sandbox
                    sandbox = await sandbox_task
                    if sandbox is not None:
                        sandbox_elapsed = time.time() - scrape_start
                        await deploy_log(f"Sandbox ready (pre-created during scrape/generate, {sandbox_elapsed:.1f}s ago)")

                    sb, project_dir = await deploy_to_sandbox(
                        generated_files, extra_deps, sandbox=sandbox, on_log=deploy_log,
                    )

                    if sb is None:
                        await deploy_log("DAYTONA_API_KEY not set — skipping deployment")
                        return

                    # Build with retry
                    build_ok, generated_files, extra_deps = await build_with_retry(
                        sb, project_dir, generated_files, [], extra_deps,
                        api_key, on_log=deploy_log,
                    )

                    if not build_ok:
                        await deploy_log(f"All {MAX_BUILD_ATTEMPTS} build attempts failed — proceeding anyway")

                    # Capture static HTML from the export (out/) for instant history previews
                    static_html = await capture_static_html(sb, project_dir, on_log=deploy_log)

                    # Start dev server for live preview
                    preview_url = await start_preview(sb, project_dir, on_log=deploy_log)
                    deploy_elapsed = time.time() - deploy_start
                    logger.info("[deploy] COMPLETE in %.1fs — preview: %s", deploy_elapsed, preview_url)
                    await deploy_log(f"Preview URL: {preview_url}")
                except Exception as e:
                    logger.error("[deploy] FAILED for %s: %s\n%s", url_str, e, traceback.format_exc())
                    deploy_error = e
                    await deploy_log(f"Deployment error: {e}")

            deploy_task = asyncio.create_task(_run_deploy())

            # Poll the queue and stream logs to SSE while deploy runs
            while not deploy_task.done():
                try:
                    msg = await asyncio.wait_for(deploy_queue.get(), timeout=0.5)
                    yield _log(msg)
                except asyncio.TimeoutError:
                    pass

            # Drain remaining queued logs
            while not deploy_queue.empty():
                yield _log(deploy_queue.get_nowait())

            # Re-raise if deploy had an uncaught exception
            try:
                exc = deploy_task.exception()
                if exc and deploy_error is None:
                    raise exc
            except (asyncio.CancelledError, asyncio.InvalidStateError):
                pass

            generated_code = generated_files.get("src/app/page.tsx", "")

            # ── DONE ──────────────────────────────────────────────
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
                url_str, total_elapsed, len(generated_code), preview_url or "none",
            )

            db_fields = dict(status="done", preview_url=preview_url, generated_code=generated_code, completed_at=datetime.now(timezone.utc).isoformat())
            if static_html:
                db_fields["static_html"] = static_html
            _db_update_clone(clone_id, **db_fields)

            yield _sse_event({
                "status": "done",
                "code": generated_code,
                "preview_url": preview_url,
                "clone_id": clone_id,
                "files": project_files,
                "static_html": static_html,
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
            yield _log("Installing dependencies...")
            npm_start = time.time()
            await asyncio.to_thread(
                sandbox.process.exec,
                f"cd {project_dir} && npm install",
            )
            logger.info("[redeploy] npm install completed in %.1fs", time.time() - npm_start)
            yield _log(f"Dependencies installed in {time.time() - npm_start:.1f}s")

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
            preview_url = await start_preview(sandbox, project_dir)
            yield _log(f"Preview ready: {preview_url}")

            # Update the stored preview URL
            _db_update_clone(clone_id, preview_url=preview_url)

            yield _sse_event({"status": "done", "preview_url": preview_url})

        except Exception as e:
            logger.error("[redeploy] FAILED for clone %s: %s\n%s", clone_id, e, traceback.format_exc())
            yield _sse_event({"status": "error", "message": str(e)})

    return StreamingResponse(stream(), media_type="text/event-stream")
