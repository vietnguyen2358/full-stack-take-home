const express = require("express");
const fs = require("fs");
const path = require("path");

const app = express();
app.use(express.json());

// Load component registry
const registry = JSON.parse(
  fs.readFileSync(path.join(__dirname, "registry.json"), "utf-8")
);

// Health check
app.get("/health", (_req, res) => {
  res.json({ status: "ok", components: registry.components.length });
});

// ── Tool definitions (MCP protocol format) ──────────────────
const TOOLS = [
  {
    name: "list_components",
    description: "List all available shadcn/ui component names and descriptions",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "get_component",
    description: "Get full details for a single shadcn/ui component: HTML pattern, Tailwind classes, variants, and sub-components",
    inputSchema: {
      type: "object",
      properties: { name: { type: "string", description: "Component name (e.g. 'button', 'card')" } },
      required: ["name"],
    },
  },
  {
    name: "get_components",
    description: "Batch lookup: get details for multiple shadcn/ui components at once",
    inputSchema: {
      type: "object",
      properties: { names: { type: "array", items: { type: "string" }, description: "Array of component names" } },
      required: ["names"],
    },
  },
  {
    name: "get_design_tokens",
    description: "Get shadcn/ui CSS design tokens: colors (light/dark), typography, spacing, and border radius values",
    inputSchema: { type: "object", properties: {} },
  },
];

// ── Tool execution ───────────────────────────────────────────
function executeTool(name, args) {
  switch (name) {
    case "list_components":
      return registry.components.map((c) => ({
        name: c.name,
        description: c.description,
      }));

    case "get_component": {
      const component = registry.components.find((c) => c.name === args?.name);
      if (!component) return { error: `Component not found: ${args?.name}` };
      return component;
    }

    case "get_components": {
      if (!Array.isArray(args?.names)) return { error: "Missing required param: names (array)" };
      return args.names
        .map((n) => registry.components.find((c) => c.name === n))
        .filter(Boolean);
    }

    case "get_design_tokens":
      return registry.design_tokens;

    default:
      return null;
  }
}

// ── JSON-RPC endpoint (MCP protocol) ─────────────────────────
app.post("/mcp", (req, res) => {
  const { jsonrpc, id, method, params } = req.body;

  if (jsonrpc !== "2.0") {
    return res.status(400).json({
      jsonrpc: "2.0",
      id,
      error: { code: -32600, message: "Invalid JSON-RPC version" },
    });
  }

  try {
    let result;

    switch (method) {
      // ── MCP protocol methods ─────────────────────────────
      case "initialize":
        result = {
          protocolVersion: "2024-11-05",
          capabilities: { tools: {} },
          serverInfo: { name: "shadcn-mcp-server", version: "1.0.0" },
        };
        break;

      case "notifications/initialized":
        // Acknowledgement — no result needed
        return res.json({ jsonrpc: "2.0", id, result: null });

      case "tools/list":
        result = { tools: TOOLS };
        break;

      case "tools/call": {
        const toolName = params?.name;
        const toolArgs = params?.arguments || {};
        const toolResult = executeTool(toolName, toolArgs);
        if (toolResult === null) {
          return res.json({
            jsonrpc: "2.0",
            id,
            error: { code: -32602, message: `Unknown tool: ${toolName}` },
          });
        }
        result = {
          content: [{ type: "text", text: JSON.stringify(toolResult, null, 2) }],
        };
        break;
      }

      // ── Direct tool calls (convenience) ──────────────────
      case "list_components":
      case "get_component":
      case "get_components":
      case "get_design_tokens": {
        result = executeTool(method, params);
        if (result === null) {
          return res.json({
            jsonrpc: "2.0",
            id,
            error: { code: -32601, message: `Method not found: ${method}` },
          });
        }
        break;
      }

      default:
        return res.json({
          jsonrpc: "2.0",
          id,
          error: { code: -32601, message: `Method not found: ${method}` },
        });
    }

    res.json({ jsonrpc: "2.0", id, result });
  } catch (err) {
    res.json({
      jsonrpc: "2.0",
      id,
      error: { code: -32603, message: err.message },
    });
  }
});

const PORT = process.env.PORT || 8001;
app.listen(PORT, () => {
  console.log(`MCP server running on port ${PORT}`);
  console.log(`Registry loaded: ${registry.components.length} components`);
});
