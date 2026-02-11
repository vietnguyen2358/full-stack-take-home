"use client";

import { useState, useRef, useEffect, FormEvent, useMemo } from "react";
import { Highlight, themes } from "prism-react-renderer";

type Phase = "idle" | "scraping" | "generating" | "deploying" | "fixing" | "done" | "error";

type CloneRecord = {
  id: string;
  url: string;
  status: string;
  preview_url: string | null;
  screenshot_count: number;
  image_count: number;
  created_at: string;
  completed_at: string | null;
};

// ── File tree types ──────────────────────────────────────────
type TreeNode = {
  name: string;
  path: string;
  children?: TreeNode[];
};

function buildTree(paths: string[]): TreeNode[] {
  const root: TreeNode[] = [];

  for (const path of paths.sort()) {
    const parts = path.split("/");
    let current = root;

    for (let i = 0; i < parts.length; i++) {
      const name = parts[i];
      const fullPath = parts.slice(0, i + 1).join("/");
      const isFile = i === parts.length - 1;

      let existing = current.find((n) => n.name === name);
      if (!existing) {
        existing = { name, path: fullPath, ...(isFile ? {} : { children: [] }) };
        current.push(existing);
      }
      if (!isFile) {
        current = existing.children!;
      }
    }
  }
  return root;
}

function FileTreeNode({
  node,
  depth,
  selectedPath,
  onSelect,
  defaultOpen,
}: {
  node: TreeNode;
  depth: number;
  selectedPath: string;
  onSelect: (path: string) => void;
  defaultOpen: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const isFolder = !!node.children;
  const isSelected = node.path === selectedPath;

  if (isFolder) {
    return (
      <div>
        <button
          onClick={() => setOpen(!open)}
          className="flex items-center gap-1.5 w-full text-left py-0.5 hover:bg-zinc-800/50 rounded px-1 transition-colors"
          style={{ paddingLeft: depth * 12 + 4 }}
        >
          <span className="text-zinc-500 text-xs w-3 text-center shrink-0">
            {open ? "▾" : "▸"}
          </span>
          <svg className="w-4 h-4 shrink-0 text-zinc-500" viewBox="0 0 20 20" fill="currentColor">
            <path d="M2 6a2 2 0 012-2h5l2 2h5a2 2 0 012 2v6a2 2 0 01-2 2H4a2 2 0 01-2-2V6z" />
          </svg>
          <span className="text-xs text-zinc-400 truncate">{node.name}</span>
        </button>
        {open && node.children!.map((child) => (
          <FileTreeNode
            key={child.path}
            node={child}
            depth={depth + 1}
            selectedPath={selectedPath}
            onSelect={onSelect}
            defaultOpen={defaultOpen}
          />
        ))}
      </div>
    );
  }

  const ext = node.name.split(".").pop() || "";
  const iconColor =
    ext === "tsx" || ext === "ts" ? "text-blue-400" :
    ext === "json" ? "text-yellow-400" :
    ext === "css" ? "text-purple-400" :
    ext === "mjs" ? "text-green-400" :
    "text-zinc-500";

  return (
    <button
      onClick={() => onSelect(node.path)}
      className={`flex items-center gap-1.5 w-full text-left py-0.5 rounded px-1 transition-colors ${
        isSelected ? "bg-zinc-800 text-white" : "hover:bg-zinc-800/50 text-zinc-400"
      }`}
      style={{ paddingLeft: depth * 12 + 4 + 16 }}
    >
      <svg className={`w-3.5 h-3.5 shrink-0 ${iconColor}`} viewBox="0 0 20 20" fill="currentColor">
        <path fillRule="evenodd" d="M4 4a2 2 0 012-2h4.586A2 2 0 0112 2.586L15.414 6A2 2 0 0116 7.414V16a2 2 0 01-2 2H6a2 2 0 01-2-2V4z" clipRule="evenodd" />
      </svg>
      <span className="text-xs truncate">{node.name}</span>
    </button>
  );
}

function getLang(path: string): string {
  const ext = path.split(".").pop() || "";
  const map: Record<string, string> = {
    tsx: "tsx", ts: "typescript", js: "javascript", jsx: "jsx",
    json: "json", css: "css", mjs: "javascript", html: "markup",
  };
  return map[ext] || "typescript";
}

// ── Main component ───────────────────────────────────────────
export default function Home() {
  const [url, setUrl] = useState("");
  const [code, setCode] = useState("");
  const [files, setFiles] = useState<Record<string, string>>({});
  const [previewUrl, setPreviewUrl] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [error, setError] = useState("");
  const [tab, setTab] = useState<"preview" | "code">("preview");
  const [selectedFile, setSelectedFile] = useState("src/app/page.tsx");
  const [statusMessage, setStatusMessage] = useState("");
  const [logs, setLogs] = useState<string[]>([]);
  const [history, setHistory] = useState<CloneRecord[]>([]);
  const [cloneId, setCloneId] = useState("");
  const logEndRef = useRef<HTMLDivElement>(null);

  // Auto-scroll activity log to bottom
  useEffect(() => {
    logEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [logs]);

  // Fetch clone history on mount
  useEffect(() => {
    fetch(`${process.env.NEXT_PUBLIC_API_URL}/clones`)
      .then((r) => r.json())
      .then((data) => { if (data.clones) setHistory(data.clones); })
      .catch(() => {}); // silently fail if DB not configured
  }, []);

  const fileTree = useMemo(() => buildTree(Object.keys(files)), [files]);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError("");
    setCode("");
    setFiles({});
    setPreviewUrl("");
    setStatusMessage("");
    setLogs([]);
    setPhase("scraping");
    setSelectedFile("src/app/page.tsx");

    try {
      const res = await fetch(`${process.env.NEXT_PUBLIC_API_URL}/clone`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url }),
      });

      if (!res.ok) {
        const text = await res.text();
        let detail = `Request failed (${res.status})`;
        try {
          const parsed = JSON.parse(text);
          if (parsed.detail) detail = parsed.detail;
        } catch {}
        throw new Error(detail);
      }

      const reader = res.body?.getReader();
      if (!reader) throw new Error("No response body");

      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";

        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          const payload = JSON.parse(line.slice(6));

          // Log events
          if (payload.log) {
            setLogs((prev) => [...prev, payload.log]);
          }

          if (payload.status === "error") {
            throw new Error(payload.message || "Something went wrong");
          }

          if (payload.status === "done") {
            setCode(payload.code);
            if (payload.files) setFiles(payload.files);
            if (payload.preview_url) setPreviewUrl(payload.preview_url);
            if (payload.clone_id) setCloneId(payload.clone_id);
            setPhase("done");
            // Refresh history
            fetch(`${process.env.NEXT_PUBLIC_API_URL}/clones`)
              .then((r) => r.json())
              .then((data) => { if (data.clones) setHistory(data.clones); })
              .catch(() => {});
          } else if (payload.status) {
            setPhase(payload.status);
            if (payload.message) setStatusMessage(payload.message);
          }
        }
      }
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Something went wrong");
      setPhase("error");
    }
  }

  function handleReset() {
    setPhase("idle");
    setCode("");
    setFiles({});
    setPreviewUrl("");
    setError("");
    setUrl("");
    setTab("preview");
    setSelectedFile("src/app/page.tsx");
    setStatusMessage("");
    setLogs([]);
    setCloneId("");
  }

  const [redeploying, setRedeploying] = useState(false);
  const [redeployLogs, setRedeployLogs] = useState<string[]>([]);

  async function handleRedeploy() {
    if (!cloneId) return;
    setRedeploying(true);
    setRedeployLogs([]);
    try {
      const res = await fetch(`${process.env.NEXT_PUBLIC_API_URL}/clones/${cloneId}/redeploy`, {
        method: "POST",
      });
      if (!res.ok) throw new Error(`Request failed (${res.status})`);

      const reader = res.body?.getReader();
      if (!reader) throw new Error("No response body");

      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";

        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          const payload = JSON.parse(line.slice(6));

          if (payload.log) {
            setRedeployLogs((prev) => [...prev, payload.log]);
          }
          if (payload.status === "done" && payload.preview_url) {
            setPreviewUrl(payload.preview_url);
            setTab("preview");
          }
          if (payload.status === "error") {
            setRedeployLogs((prev) => [...prev, `Error: ${payload.message}`]);
          }
        }
      }
    } catch (err: unknown) {
      setRedeployLogs((prev) => [...prev, `Error: ${err instanceof Error ? err.message : "Failed"}`]);
    } finally {
      setRedeploying(false);
    }
  }

  const isLoading = phase === "scraping" || phase === "generating" || phase === "deploying" || phase === "fixing";

  const selectedFileContent = files[selectedFile] || "";

  // ---------- Result view (after clone is done) ----------
  if (phase === "done" && code) {
    return (
      <div className="flex flex-col h-screen">
        {/* Top bar */}
        <div className="flex items-center gap-3 px-5 py-3 border-b border-zinc-800 bg-zinc-950">
          <button
            onClick={handleReset}
            className="text-sm font-semibold tracking-tight text-white hover:text-zinc-300 transition-colors"
          >
            Clone
          </button>
          <div className="h-4 w-px bg-zinc-700" />

          {/* Preview / Code toggle */}
          <div className="flex rounded-md bg-zinc-900 p-0.5">
            <button
              onClick={() => setTab("preview")}
              className={`text-xs font-medium px-3 py-1 rounded transition-colors ${
                tab === "preview"
                  ? "bg-zinc-700 text-white"
                  : "text-zinc-400 hover:text-zinc-200"
              }`}
            >
              Preview
            </button>
            <button
              onClick={() => setTab("code")}
              className={`text-xs font-medium px-3 py-1 rounded transition-colors ${
                tab === "code"
                  ? "bg-zinc-700 text-white"
                  : "text-zinc-400 hover:text-zinc-200"
              }`}
            >
              Code
            </button>
          </div>

          <span className="text-sm text-zinc-500 truncate flex-1">{url}</span>
          <button
            onClick={handleReset}
            className="text-sm px-3 py-1.5 rounded-md bg-zinc-800 text-zinc-300 hover:bg-zinc-700 hover:text-white transition-colors"
          >
            Clone another
          </button>
        </div>

        {/* Preview — always mounted to preserve Daytona auth handshake */}
        {previewUrl ? (
          <iframe
            src={previewUrl}
            sandbox="allow-scripts allow-same-origin"
            className={`flex-1 w-full border-none bg-white ${tab !== "preview" ? "hidden" : ""}`}
            title="Cloned website"
          />
        ) : (
          tab === "preview" && (
            <div className="flex-1 flex items-center justify-center bg-zinc-950">
              <div className="text-center space-y-4">
                {redeploying ? (
                  <div className="w-full max-w-2xl mx-auto flex flex-col items-center gap-6 px-4">
                    <div className="flex flex-col items-center gap-3">
                      <div className="w-8 h-8 border-2 border-zinc-700 border-t-white rounded-full" style={{ animation: "spin-slow 1s linear infinite" }} />
                      <p className="text-sm font-medium text-white">Re-deploying to sandbox...</p>
                    </div>
                    {redeployLogs.length > 0 && (
                      <div className="w-full rounded-lg border border-zinc-800 bg-zinc-900/50 overflow-hidden">
                        <div className="flex items-center gap-2 px-4 py-2 border-b border-zinc-800 bg-zinc-900">
                          <div className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse" />
                          <span className="text-xs font-medium text-zinc-400">Activity</span>
                          <span className="text-xs text-zinc-600 ml-auto">{redeployLogs.length} events</span>
                        </div>
                        <div className="max-h-52 overflow-y-auto p-3 space-y-0.5 text-left">
                          {redeployLogs.map((log, i) => (
                            <p key={i} className="text-xs font-mono leading-relaxed text-zinc-500">
                              <span className="text-zinc-700 select-none mr-2">{String(i + 1).padStart(2, "0")}</span>
                              {log}
                            </p>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                ) : (
                  <>
                    <p className="text-zinc-400 text-sm">
                      Sandbox preview has expired.
                    </p>
                    {cloneId && (
                      <button
                        onClick={handleRedeploy}
                        className="px-4 py-2 rounded-lg bg-white text-black text-sm font-semibold hover:bg-zinc-200 transition-colors"
                      >
                        Re-deploy to sandbox
                      </button>
                    )}
                    <p className="text-zinc-500 text-xs">
                      Or switch to the Code tab to view the source.
                    </p>
                  </>
                )}
              </div>
            </div>
          )
        )}

        {/* Code view with file tree */}
        {tab === "code" && (
          <div className="flex-1 flex overflow-hidden bg-zinc-950">
            {/* File tree sidebar */}
            <div className="w-60 shrink-0 border-r border-zinc-800 flex flex-col overflow-hidden">
              <div className="px-3 py-2 border-b border-zinc-800 bg-zinc-900/50">
                <span className="text-xs font-medium text-zinc-500 uppercase tracking-wider">Files</span>
              </div>
              <div className="flex-1 overflow-y-auto py-1 px-1">
                {fileTree.map((node) => (
                  <FileTreeNode
                    key={node.path}
                    node={node}
                    depth={0}
                    selectedPath={selectedFile}
                    onSelect={setSelectedFile}
                    defaultOpen={true}
                  />
                ))}
              </div>
            </div>

            {/* Code panel */}
            <div className="flex-1 flex flex-col overflow-hidden">
              <div className="flex items-center justify-between px-5 py-2 border-b border-zinc-800">
                <span className="text-xs text-zinc-500 font-mono">{selectedFile}</span>
                <button
                  onClick={() => {
                    navigator.clipboard.writeText(selectedFileContent);
                  }}
                  className="text-xs px-2 py-1 rounded bg-zinc-800 text-zinc-400 hover:text-white hover:bg-zinc-700 transition-colors"
                >
                  Copy
                </button>
              </div>
              <div className="flex-1 overflow-auto">
                <Highlight theme={themes.nightOwl} code={selectedFileContent} language={getLang(selectedFile)}>
                  {({ style, tokens, getLineProps, getTokenProps }) => (
                    <pre style={{ ...style, margin: 0, padding: "1.25rem", background: "transparent" }} className="text-xs font-mono leading-relaxed">
                      {tokens.map((line, i) => (
                        <div key={i} {...getLineProps({ line })} className="table-row">
                          <span className="table-cell pr-4 select-none text-right text-zinc-600 w-10">{i + 1}</span>
                          <span className="table-cell">
                            {line.map((token, key) => (
                              <span key={key} {...getTokenProps({ token })} />
                            ))}
                          </span>
                        </div>
                      ))}
                    </pre>
                  )}
                </Highlight>
              </div>
            </div>
          </div>
        )}
      </div>
    );
  }

  // ---------- Landing / Loading / Error view ----------
  return (
    <div className="flex flex-col items-center justify-center min-h-screen px-6">
      {/* Hero */}
      <div className="flex flex-col items-center gap-4 mb-10 text-center">
        <h1 className="text-5xl font-bold tracking-tight text-white">
          Clone
        </h1>
        <p className="text-lg text-zinc-400 max-w-md">
          Paste any URL and get an AI-generated replica in seconds.
        </p>
      </div>

      {/* URL Input */}
      <form
        onSubmit={handleSubmit}
        className="w-full max-w-xl flex items-center gap-2"
      >
        <div className="relative flex-1">
          <input
            type="url"
            required
            placeholder="https://example.com"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            disabled={isLoading}
            className="w-full h-12 pl-4 pr-4 rounded-lg border border-zinc-700 bg-zinc-900 text-white placeholder:text-zinc-500 focus:outline-none focus:ring-2 focus:ring-zinc-500 focus:border-transparent disabled:opacity-50 transition-all"
          />
        </div>
        <button
          type="submit"
          disabled={isLoading}
          className="h-12 px-6 rounded-lg bg-white text-black font-semibold hover:bg-zinc-200 disabled:opacity-50 transition-colors shrink-0"
        >
          {isLoading ? "Cloning..." : "Clone"}
        </button>
      </form>

      {/* Progress + Activity Log */}
      {isLoading && (
        <div className="mt-8 w-full max-w-2xl flex flex-col items-center gap-6">
          {/* Spinner + Step indicator */}
          <div className="flex flex-col items-center gap-4">
            <div className="w-8 h-8 border-2 border-zinc-700 border-t-white rounded-full" style={{ animation: "spin-slow 1s linear infinite" }} />

            <div className="flex items-center gap-3">
              {[
                { key: "scraping", label: "Scrape" },
                { key: "generating", label: "Generate" },
                { key: "deploying", label: "Deploy" },
              ].map((step, i) => {
                const steps = ["scraping", "generating", "deploying"];
                const currentIdx = steps.indexOf(phase);
                const isActive = step.key === phase;
                const isCompleted = i < currentIdx;
                return (
                  <div key={step.key} className="flex items-center gap-3">
                    {i > 0 && (
                      <div className={`w-8 h-px ${isCompleted || isActive ? "bg-white" : "bg-zinc-700"}`} />
                    )}
                    <div className="flex items-center gap-2">
                      <div className={`w-6 h-6 rounded-full flex items-center justify-center text-xs font-medium border ${
                        isActive
                          ? "border-white text-white"
                          : isCompleted
                          ? "border-white bg-white text-black"
                          : "border-zinc-700 text-zinc-600"
                      }`}>
                        {isCompleted ? "✓" : i + 1}
                      </div>
                      <span className={`text-xs font-medium ${
                        isActive ? "text-white" : isCompleted ? "text-zinc-400" : "text-zinc-600"
                      }`}>
                        {step.label}
                      </span>
                    </div>
                  </div>
                );
              })}
            </div>

            <p className="text-sm font-medium text-white">
              {statusMessage || (
                phase === "scraping" ? "Scraping website..." :
                phase === "generating" ? "Generating clone with AI..." :
                phase === "fixing" ? "Fixing build errors..." :
                "Deploying to sandbox..."
              )}
            </p>
          </div>

          {/* Activity Log */}
          {logs.length > 0 && (
            <div className="w-full rounded-lg border border-zinc-800 bg-zinc-900/50 overflow-hidden">
              <div className="flex items-center gap-2 px-4 py-2 border-b border-zinc-800 bg-zinc-900">
                <div className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse" />
                <span className="text-xs font-medium text-zinc-400">Activity</span>
                <span className="text-xs text-zinc-600 ml-auto">{logs.length} events</span>
              </div>
              <div className="max-h-52 overflow-y-auto p-3 space-y-0.5">
                {logs.map((log, i) => (
                  <p key={i} className="text-xs font-mono leading-relaxed text-zinc-500">
                    <span className="text-zinc-700 select-none mr-2">{String(i + 1).padStart(2, "0")}</span>
                    {log}
                  </p>
                ))}
                <div ref={logEndRef} />
              </div>
            </div>
          )}
        </div>
      )}

      {/* Error */}
      {phase === "error" && (
        <div className="mt-6 w-full max-w-2xl space-y-3">
          {error && (
            <div className="px-4 py-3 rounded-lg bg-red-950/50 border border-red-900/50">
              <p className="text-sm text-red-400">{error}</p>
            </div>
          )}

          {/* Show activity log on error too so user can see what happened */}
          {logs.length > 0 && (
            <div className="w-full rounded-lg border border-zinc-800 bg-zinc-900/50 overflow-hidden">
              <div className="flex items-center gap-2 px-4 py-2 border-b border-zinc-800 bg-zinc-900">
                <div className="w-1.5 h-1.5 rounded-full bg-red-500" />
                <span className="text-xs font-medium text-zinc-400">Activity Log</span>
              </div>
              <div className="max-h-52 overflow-y-auto p-3 space-y-0.5">
                {logs.map((log, i) => (
                  <p key={i} className="text-xs font-mono leading-relaxed text-zinc-500">
                    <span className="text-zinc-700 select-none mr-2">{String(i + 1).padStart(2, "0")}</span>
                    {log}
                  </p>
                ))}
              </div>
            </div>
          )}

          <button
            onClick={handleReset}
            className="text-sm text-zinc-400 hover:text-white transition-colors"
          >
            Try again
          </button>
        </div>
      )}

      {/* Clone History */}
      {!isLoading && phase !== "error" && history.length > 0 && (
        <div className="mt-12 w-full max-w-2xl">
          <h2 className="text-sm font-medium text-zinc-500 mb-3">Recent clones</h2>
          <div className="space-y-2">
            {history.map((clone) => (
              <div
                key={clone.id}
                className={`flex items-center gap-3 px-4 py-3 rounded-lg border border-zinc-800 bg-zinc-900/50 transition-colors group ${
                  clone.status === "done" ? "hover:bg-zinc-900" : "opacity-60"
                }`}
              >
                <button
                  disabled={clone.status !== "done"}
                  onClick={() => {
                    fetch(`${process.env.NEXT_PUBLIC_API_URL}/clones/${clone.id}`)
                      .then((r) => r.json())
                      .then((data) => {
                        setUrl(data.url);
                        setCode(data.generated_code || "");
                        setFiles(data.files || {});
                        setPreviewUrl("");
                        setCloneId(data.id);
                        setTab("code");
                        setPhase("done");
                      })
                      .catch(() => {});
                  }}
                  className={`flex items-center gap-3 flex-1 min-w-0 text-left ${
                    clone.status === "done" ? "cursor-pointer" : "cursor-not-allowed"
                  }`}
                >
                  <div className={`w-2 h-2 rounded-full shrink-0 ${
                    clone.status === "done" ? "bg-emerald-500" :
                    clone.status === "error" ? "bg-red-500" :
                    "bg-yellow-500 animate-pulse"
                  }`} />
                  <span className="text-sm text-zinc-300 truncate flex-1">{clone.url}</span>
                  <span className="text-xs text-zinc-600 shrink-0">
                    {new Date(clone.created_at).toLocaleDateString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}
                  </span>
                </button>
                <button
                  onClick={() => {
                    fetch(`${process.env.NEXT_PUBLIC_API_URL}/clones/${clone.id}`, { method: "DELETE" })
                      .then(() => setHistory((prev) => prev.filter((c) => c.id !== clone.id)))
                      .catch(() => {});
                  }}
                  className="opacity-0 group-hover:opacity-100 text-zinc-600 hover:text-red-400 transition-all shrink-0 p-1"
                  title="Delete clone"
                >
                  <svg className="w-4 h-4" viewBox="0 0 20 20" fill="currentColor">
                    <path fillRule="evenodd" d="M9 2a1 1 0 00-.894.553L7.382 4H4a1 1 0 000 2v10a2 2 0 002 2h8a2 2 0 002-2V6a1 1 0 100-2h-3.382l-.724-1.447A1 1 0 0011 2H9zM7 8a1 1 0 012 0v6a1 1 0 11-2 0V8zm5-1a1 1 0 00-1 1v6a1 1 0 102 0V8a1 1 0 00-1-1z" clipRule="evenodd" />
                  </svg>
                </button>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
