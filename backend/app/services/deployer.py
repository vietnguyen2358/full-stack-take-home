import asyncio
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from daytona import CreateSandboxFromImageParams, Daytona, DaytonaConfig

from app.services.ai import fix_build_errors

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).resolve().parent.parent.parent / "sandbox-template"

# Files to upload from the template (relative to TEMPLATE_DIR)
TEMPLATE_FILES = [
    "package.json",
    "next.config.mjs",
    "tsconfig.json",
    "postcss.config.mjs",
    "src/lib/utils.ts",
    "src/app/layout.tsx",
    "src/app/globals.css",
    "src/app/[...slug]/page.tsx",
]

MAX_BUILD_ATTEMPTS = 3

# Timeouts for sandbox operations (seconds)
EXEC_TIMEOUT_MKDIR = 30
EXEC_TIMEOUT_INSTALL = 180
EXEC_TIMEOUT_BUILD = 120
EXEC_TIMEOUT_START = 15
EXEC_TIMEOUT_CHECK = 10

# Async log callback type
LogCallback = Callable[[str], Awaitable[None]]


async def _sandbox_exec(sandbox: Any, cmd: str, timeout: int) -> Any:
    """Run a command in the sandbox with a hard timeout.

    Returns the exec result, or raises asyncio.TimeoutError if it stalls.
    """
    return await asyncio.wait_for(
        asyncio.to_thread(sandbox.process.exec, cmd),
        timeout=timeout,
    )


def _get_daytona_client():
    """Return a configured Daytona client, or None if not configured."""
    api_key = os.environ.get("DAYTONA_API_KEY")
    if not api_key:
        return None
    config = DaytonaConfig(
        api_key=api_key,
        api_url=os.environ.get("DAYTONA_API_URL", "https://app.daytona.io/api"),
        target=os.environ.get("DAYTONA_TARGET", "us"),
    )
    return Daytona(config)


def _cleanup_old_sandboxes(daytona):
    """Delete all existing sandboxes to stay within free tier limits."""
    try:
        existing = daytona.list()
        sandboxes = existing.items if hasattr(existing, 'items') else existing
        for sb in sandboxes:
            try:
                daytona.delete(sb, timeout=10)
                logger.info("[deploy] Deleted old sandbox %s", getattr(sb, 'id', '?'))
            except Exception as e:
                logger.warning("[deploy] Failed to delete sandbox: %s", e)
    except Exception as e:
        logger.warning("[deploy] Failed to list sandboxes for cleanup: %s", e)


def _create_sandbox():
    """Create and return a Daytona sandbox, or None if not configured."""
    daytona = _get_daytona_client()
    if daytona is None:
        return None

    params = CreateSandboxFromImageParams(
        image="node:20",
        auto_delete_interval=1200,
        public=True,
    )
    return daytona.create(params)


async def deploy_to_sandbox(
    generated_files: dict[str, str],
    extra_deps: list[str],
    sandbox: Any | None = None,
    on_log: LogCallback | None = None,
) -> tuple[Any | None, str]:
    """Upload template + generated files to sandbox and npm install.

    Args:
        generated_files: Dict of path->content for AI-generated files.
        extra_deps: Additional npm packages requested by AI.
        sandbox: Pre-created sandbox. If None, creates a new one.
        on_log: Async callback for progress messages.

    Returns:
        (sandbox, project_dir) — sandbox is None if DAYTONA_API_KEY not set.
    """
    project_dir = "/home/daytona/app"

    if sandbox is None:
        sandbox_start = time.time()
        sandbox = await asyncio.to_thread(_create_sandbox)
        if sandbox is None:
            logger.warning("[deploy] DAYTONA_API_KEY not set — skipping deployment")
            return None, project_dir
        logger.info("[deploy] Sandbox created in %.1fs", time.time() - sandbox_start)
    else:
        logger.info("[deploy] Using pre-created sandbox")

    # Create all directories needed
    all_dirs = {"src/app/[...slug]", "src/lib"}
    for gen_path in generated_files:
        parent = str(Path(gen_path).parent)
        if parent and parent != ".":
            all_dirs.add(parent)
    mkdir_cmd = " ".join(f"'{project_dir}/{d}'" for d in sorted(all_dirs))
    await _sandbox_exec(sandbox, f"mkdir -p {mkdir_cmd}", EXEC_TIMEOUT_MKDIR)

    # Upload all files in parallel
    total_files = len(TEMPLATE_FILES) + len(generated_files)
    if on_log:
        await on_log(f"Uploading {total_files} files...")

    upload_start = time.time()
    upload_tasks = []

    for rel_path in TEMPLATE_FILES:
        file_path = TEMPLATE_DIR / rel_path
        upload_tasks.append(asyncio.to_thread(
            sandbox.fs.upload_file,
            file_path.read_bytes(),
            f"{project_dir}/{rel_path}",
        ))
    for gen_path, gen_content in generated_files.items():
        upload_tasks.append(asyncio.to_thread(
            sandbox.fs.upload_file,
            gen_content.encode("utf-8"),
            f"{project_dir}/{gen_path}",
        ))

    await asyncio.gather(*upload_tasks)

    upload_elapsed = time.time() - upload_start
    logger.info("[deploy] Uploaded %d files in %.1fs", total_files, upload_elapsed)
    if on_log:
        await on_log(f"Uploaded {total_files} files in {upload_elapsed:.1f}s")

    # npm install (base + any extra AI-requested deps)
    safe_deps: list[str] = []
    if extra_deps:
        safe_deps = [d for d in extra_deps if re.match(r'^[@a-zA-Z0-9][\w./@-]*$', d)]

    npm_cmd = f"cd {project_dir} && npm install"
    if safe_deps:
        npm_cmd += f" && npm install {' '.join(safe_deps)}"

    if on_log:
        msg = "Installing dependencies"
        if safe_deps:
            preview = ", ".join(safe_deps[:3])
            msg += f" + {len(safe_deps)} extra ({preview}{'...' if len(safe_deps) > 3 else ''})"
        await on_log(msg + "...")

    npm_start = time.time()
    try:
        await _sandbox_exec(sandbox, npm_cmd, EXEC_TIMEOUT_INSTALL)
    except asyncio.TimeoutError:
        logger.warning("[deploy] npm install timed out after %ds", EXEC_TIMEOUT_INSTALL)
        if on_log:
            await on_log(f"npm install timed out after {EXEC_TIMEOUT_INSTALL}s — continuing anyway")
    npm_elapsed = time.time() - npm_start
    logger.info("[deploy] npm install completed in %.1fs", npm_elapsed)
    if on_log:
        await on_log(f"Dependencies installed in {npm_elapsed:.1f}s")

    return sandbox, project_dir


async def build_with_retry(
    sandbox: Any,
    project_dir: str,
    generated_files: dict[str, str],
    content: list[dict],
    extra_deps: list[str],
    api_key: str,
    on_log: LogCallback | None = None,
) -> tuple[bool, dict[str, str], list[str]]:
    """Run next build with up to MAX_BUILD_ATTEMPTS retries, asking AI to fix errors.

    Returns:
        (build_ok, final_generated_files, final_extra_deps).
    """
    build_ok = False
    for attempt in range(1, MAX_BUILD_ATTEMPTS + 1):
        # Re-upload all generated files before each retry
        if attempt > 1:
            if on_log:
                await on_log(f"Re-uploading fixed files (attempt {attempt}/{MAX_BUILD_ATTEMPTS})...")
            for gen_path, gen_content in generated_files.items():
                await asyncio.to_thread(
                    sandbox.fs.upload_file,
                    gen_content.encode("utf-8"),
                    f"{project_dir}/{gen_path}",
                )

        if on_log:
            label = "Running next build"
            if attempt > 1:
                label += f" (attempt {attempt}/{MAX_BUILD_ATTEMPTS})"
            await on_log(label + "...")

        # Run build with periodic heartbeat + hard timeout
        build_start = time.time()
        heartbeat_active = True

        async def _heartbeat():
            while heartbeat_active:
                await asyncio.sleep(5)
                if heartbeat_active and on_log:
                    elapsed = time.time() - build_start
                    await on_log(f"  Still building... ({elapsed:.0f}s)")

        heartbeat_task = asyncio.create_task(_heartbeat())
        timed_out = False
        try:
            build_result = await _sandbox_exec(
                sandbox,
                f"cd {project_dir} && npx next build 2>&1",
                EXEC_TIMEOUT_BUILD,
            )
        except asyncio.TimeoutError:
            timed_out = True
            build_elapsed = time.time() - build_start
            logger.warning("[deploy] Build timed out after %.0fs on attempt %d", build_elapsed, attempt)
            if on_log:
                await on_log(f"Build timed out after {build_elapsed:.0f}s — skipping build check")
            break
        finally:
            heartbeat_active = False
            heartbeat_task.cancel()

        if timed_out:
            break

        build_output = build_result.result or ""
        build_elapsed = time.time() - build_start
        if build_result.exit_code == 0:
            logger.info("[deploy] Build succeeded on attempt %d in %.1fs", attempt, build_elapsed)
            if on_log:
                await on_log(f"Build succeeded in {build_elapsed:.1f}s")
            build_ok = True
            break

        # Build failed — extract error lines
        error_lines = []
        for line in build_output.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if any(noise in stripped.lower() for noise in [
                "telemetry", "anonymous", "opt-out", "nextjs.org",
                "collecting page data", "generating static pages",
                "finalizing page optimization",
                "creating an optimized production build",
                "compiled successfully",
            ]):
                continue
            error_lines.append(line)
        error_text = "\n".join(error_lines[-100:])
        if not error_text.strip():
            error_text = build_output[-3000:]
        logger.warning("[deploy] Build failed attempt %d/%d (exit %d) in %.1fs: %s", attempt, MAX_BUILD_ATTEMPTS, build_result.exit_code, build_elapsed, error_text[:300])

        if on_log:
            await on_log(f"Build failed in {build_elapsed:.1f}s (attempt {attempt}/{MAX_BUILD_ATTEMPTS})")

        if attempt < MAX_BUILD_ATTEMPTS:
            # Skip AI fix for OOM/signal kills (exit 137, 139, etc.) — not code errors
            if build_result.exit_code >= 128:
                logger.warning("[deploy] Build killed by signal (exit %d), likely OOM — skipping AI fix", build_result.exit_code)
                if on_log:
                    await on_log(f"Build killed by signal (exit {build_result.exit_code}) — skipping retry")
                break
            try:
                if on_log:
                    await on_log("Asking AI to fix build errors...")
                generated_files, fix_deps = await fix_build_errors(
                    content, generated_files, error_text, api_key
                )
                extra_deps = list(set(extra_deps + fix_deps))
                logger.info("[deploy] AI fix attempt %d returned %d files", attempt + 1, len(generated_files))
                if on_log:
                    await on_log(f"AI returned {len(generated_files)} fixed files")
            except Exception as fix_err:
                logger.error("[deploy] AI fix request failed: %s", fix_err)
                if on_log:
                    await on_log(f"AI fix request failed: {fix_err}")
                break
        else:
            logger.warning("[deploy] All %d build attempts failed", MAX_BUILD_ATTEMPTS)

    return build_ok, generated_files, extra_deps


# Node script that inlines CSS into the exported HTML for a self-contained file.
# JS is skipped (too large) — the static preview shows the visual layout without interactivity.
_INLINE_SCRIPT = r"""
const fs = require('fs');
const path = require('path');
const outDir = process.argv[2];
const htmlPath = path.join(outDir, 'index.html');
if (!fs.existsSync(htmlPath)) { process.exit(1); }
let html = fs.readFileSync(htmlPath, 'utf8');

// Inline all CSS <link> tags
html = html.replace(/<link\s+[^>]*href="([^"]*\.css)"[^>]*\/?>/gi, (match, href) => {
  try {
    const css = fs.readFileSync(path.join(outDir, href), 'utf8');
    return '<style>' + css + '</style>';
  } catch { return match; }
});

// Remove JS script tags (too large for storage, static preview doesn't need them)
html = html.replace(/<script\s+[^>]*src="[^"]*\.js"[^>]*><\/script>/gi, '');

process.stdout.write(html);
""".strip()


async def capture_static_html(
    sandbox: Any,
    project_dir: str,
    on_log: LogCallback | None = None,
) -> str | None:
    """Bundle the static export (out/) into a single self-contained HTML string.

    Returns the HTML string, or None if export not available.
    """
    out_dir = f"{project_dir}/out"
    try:
        # Write the inline script to the sandbox
        script_path = f"{project_dir}/_inline.cjs"
        await asyncio.to_thread(
            sandbox.fs.upload_file,
            _INLINE_SCRIPT.encode("utf-8"),
            script_path,
        )
        result = await _sandbox_exec(
            sandbox,
            f"node {script_path} {out_dir}",
            EXEC_TIMEOUT_CHECK,
        )
        html = (result.result or "").strip()
        if not html or result.exit_code != 0:
            logger.warning("[deploy] Static HTML capture failed (exit %d)", result.exit_code)
            return None
        if on_log:
            await on_log(f"Static preview captured ({len(html) // 1024}KB)")
        logger.info("[deploy] Captured static HTML: %d bytes", len(html))
        return html
    except Exception as e:
        logger.warning("[deploy] Failed to capture static HTML: %s", e)
        return None


async def start_preview(
    sandbox: Any,
    project_dir: str,
    on_log: LogCallback | None = None,
) -> str:
    """Start the Next.js dev server and return the preview URL."""
    if on_log:
        await on_log("Starting Next.js dev server (turbopack)...")

    await _sandbox_exec(
        sandbox,
        f"cd {project_dir} && nohup npx next dev --turbopack -p 8080 > /tmp/next.log 2>&1 & disown",
        EXEC_TIMEOUT_START,
    )

    # Poll for server readiness instead of hard-coded 5s sleep
    start = time.time()
    ready = False
    for i in range(30):
        await asyncio.sleep(1)
        try:
            check = await _sandbox_exec(
                sandbox,
                "grep -c 'Ready in\\|ready started' /tmp/next.log 2>/dev/null || echo 0",
                EXEC_TIMEOUT_CHECK,
            )
            if check.result and check.result.strip() != "0":
                ready = True
                break
        except (asyncio.TimeoutError, Exception):
            pass

    elapsed = time.time() - start
    if ready:
        if on_log:
            await on_log(f"Dev server ready in {elapsed:.1f}s")
    else:
        # Fallback — server might be ready even if grep didn't match
        if on_log:
            await on_log(f"Dev server started ({elapsed:.1f}s)")

    preview = sandbox.create_signed_preview_url(8080, expires_in_seconds=1200)
    preview_url = preview.url
    logger.info("[deploy] Preview ready: %s", preview_url)
    return preview_url
