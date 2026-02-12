"""MCP client for the shadcn component registry server.

Communicates with the MCP server over HTTP JSON-RPC.
Gracefully degrades — returns empty results if the server is unavailable.
"""

import json
import logging
import os

import httpx

logger = logging.getLogger(__name__)

MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://localhost:8001")
MCP_ENDPOINT = f"{MCP_SERVER_URL}/mcp"

_request_id = 0
_tools_cache: list[dict] | None = None


def _next_id() -> int:
    global _request_id
    _request_id += 1
    return _request_id


async def _rpc(method: str, params: dict | None = None) -> dict | None:
    """Send a JSON-RPC 2.0 request to the MCP server."""
    payload = {
        "jsonrpc": "2.0",
        "id": _next_id(),
        "method": method,
    }
    if params is not None:
        payload["params"] = params

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(MCP_ENDPOINT, json=payload)
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                logger.warning("[mcp] RPC error for %s: %s", method, data["error"])
                return None
            return data.get("result")
    except Exception as exc:
        logger.warning("[mcp] Server unreachable (%s): %s", method, exc)
        return None


async def initialize() -> bool:
    """Initialize the MCP session. Returns True on success."""
    result = await _rpc("initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "clone-backend", "version": "1.0.0"},
    })
    if result is None:
        return False
    await _rpc("notifications/initialized")
    logger.info("[mcp] Session initialized with %s", MCP_SERVER_URL)
    return True


async def list_tools() -> list[dict]:
    """Return MCP tools in OpenAI function-calling format.

    Caches after first successful call. Returns [] if server is down.
    """
    global _tools_cache
    if _tools_cache is not None:
        return _tools_cache

    ok = await initialize()
    if not ok:
        return []

    result = await _rpc("tools/list")
    if result is None:
        return []

    mcp_tools = result.get("tools", [])
    openai_tools = []
    for t in mcp_tools:
        openai_tools.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t.get("inputSchema", {"type": "object", "properties": {}}),
            },
        })

    _tools_cache = openai_tools
    logger.info("[mcp] Loaded %d tools: %s", len(openai_tools), [t["function"]["name"] for t in openai_tools])
    return openai_tools


async def call_tool(name: str, arguments: dict) -> str:
    """Execute an MCP tool and return the text result.

    Returns an error string on failure so the model can work around it.
    """
    result = await _rpc("tools/call", {"name": name, "arguments": arguments})
    if result is None:
        return json.dumps({"error": f"MCP tool '{name}' call failed — server may be down"})

    # Extract text from content blocks
    content_blocks = result.get("content", [])
    texts = []
    for block in content_blocks:
        if block.get("type") == "text":
            texts.append(block["text"])
    return "\n".join(texts) if texts else json.dumps(result)
