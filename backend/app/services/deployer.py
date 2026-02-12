import asyncio
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable

from daytona import CreateSandboxFromImageParams, Daytona, DaytonaConfig

from app.services.ai import fix_build_errors, strip_markdown_fences, parse_multi_file_output

logger = logging.getLogger(__name__)

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

MAX_BUILD_ATTEMPTS = 3


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


async def deploy_to_sandbox(
    generated_files: dict[str, str],
    extra_deps: list[str],
) -> tuple[Any | None, str]:
    """Create sandbox, upload template + generated files, npm install.

    Returns:
        (sandbox, project_dir) — sandbox is None if DAYTONA_API_KEY not set.
    """
    project_dir = "/home/daytona/app"

    sandbox_start = time.time()
    sandbox = await asyncio.to_thread(_create_sandbox)
    if sandbox is None:
        logger.warning("[deploy] DAYTONA_API_KEY not set — skipping deployment")
        return None, project_dir

    logger.info("[deploy] Sandbox created in %.1fs", time.time() - sandbox_start)

    # Create all directories needed
    all_dirs = {"src/app/[...slug]", "src/lib"}
    for gen_path in generated_files:
        parent = str(Path(gen_path).parent)
        if parent and parent != ".":
            all_dirs.add(parent)
    mkdir_cmd = " ".join(f"{project_dir}/{d}" for d in sorted(all_dirs))
    await asyncio.to_thread(sandbox.process.exec, f"mkdir -p {mkdir_cmd}")

    # Upload template files
    upload_start = time.time()
    for rel_path in TEMPLATE_FILES:
        file_path = TEMPLATE_DIR / rel_path
        await asyncio.to_thread(
            sandbox.fs.upload_file,
            file_path.read_bytes(),
            f"{project_dir}/{rel_path}",
        )

    # Upload all AI-generated files
    for gen_path, gen_content in generated_files.items():
        await asyncio.to_thread(
            sandbox.fs.upload_file,
            gen_content.encode("utf-8"),
            f"{project_dir}/{gen_path}",
        )
    logger.info("[deploy] Uploaded %d template + %d generated files in %.1fs", len(TEMPLATE_FILES), len(generated_files), time.time() - upload_start)

    # npm install (base + any extra AI-requested deps)
    npm_cmd = f"cd {project_dir} && npm install"
    if extra_deps:
        safe_deps = [d for d in extra_deps if re.match(r'^[@a-zA-Z0-9][\w./@-]*$', d)]
        if safe_deps:
            npm_cmd += f" && npm install {' '.join(safe_deps)}"
            logger.info("[deploy] Installing base deps + %d extra: %s", len(safe_deps), ', '.join(safe_deps))

    npm_start = time.time()
    await asyncio.to_thread(sandbox.process.exec, npm_cmd)
    logger.info("[deploy] npm install completed in %.1fs", time.time() - npm_start)

    return sandbox, project_dir


async def build_with_retry(
    sandbox: Any,
    project_dir: str,
    generated_files: dict[str, str],
    content: list[dict],
    extra_deps: list[str],
    api_key: str,
    on_log: Callable[[str], str],
    on_status: Callable[[str, str], str],
) -> tuple[bool, dict[str, str], list[str]]:
    """Run next build with up to MAX_BUILD_ATTEMPTS retries, asking AI to fix errors.

    Returns:
        (build_ok, final_generated_files, final_extra_deps).
    """
    build_ok = False
    for attempt in range(1, MAX_BUILD_ATTEMPTS + 1):
        # Re-upload all generated files before each retry
        if attempt > 1:
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

        if attempt < MAX_BUILD_ATTEMPTS:
            try:
                generated_files, fix_deps = await fix_build_errors(
                    content, generated_files, error_text, api_key
                )
                extra_deps = list(set(extra_deps + fix_deps))
                logger.info("[deploy] AI fix attempt %d returned %d files", attempt + 1, len(generated_files))
            except Exception as fix_err:
                logger.error("[deploy] AI fix request failed: %s", fix_err)
                break
        else:
            logger.warning("[deploy] All %d build attempts failed", MAX_BUILD_ATTEMPTS)

    return build_ok, generated_files, extra_deps


async def start_preview(sandbox: Any, project_dir: str) -> str:
    """Start the Next.js dev server and return the preview URL."""
    await asyncio.to_thread(
        sandbox.process.exec,
        f"cd {project_dir} && nohup npx next dev -p 8080 > /tmp/next.log 2>&1 & disown",
    )
    await asyncio.sleep(5)

    preview = sandbox.create_signed_preview_url(8080)
    preview_url = preview.url
    logger.info("[deploy] Preview ready: %s", preview_url)
    return preview_url
