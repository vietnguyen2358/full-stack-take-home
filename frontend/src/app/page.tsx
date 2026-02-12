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
          className="flex items-center gap-1.5 w-full text-left py-0.5 hover:bg-surface-2/50 rounded px-1 transition-colors"
          style={{ paddingLeft: depth * 12 + 4 }}
        >
          <span className="text-neutral-500 text-xs w-3 text-center shrink-0">
            {open ? "▾" : "▸"}
          </span>
          <svg className="w-4 h-4 shrink-0 text-neutral-500" viewBox="0 0 20 20" fill="currentColor">
            <path d="M2 6a2 2 0 012-2h5l2 2h5a2 2 0 012 2v6a2 2 0 01-2 2H4a2 2 0 01-2-2V6z" />
          </svg>
          <span className="text-xs text-neutral-400 truncate">{node.name}</span>
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
    "text-neutral-500";

  return (
    <button
      onClick={() => onSelect(node.path)}
      className={`flex items-center gap-1.5 w-full text-left py-0.5 rounded px-1 transition-colors ${
        isSelected
          ? "bg-surface-2 text-neutral-50 border-r-2 border-accent"
          : "hover:bg-surface-2/50 text-neutral-400"
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

// ── SVG Arc Spinner ──────────────────────────────────────────
function ArcSpinner({ size = 32 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 50 50"
      className="animate-[spinner-rotate_2s_linear_infinite]"
    >
      <circle
        cx="25"
        cy="25"
        r="20"
        fill="none"
        stroke="var(--accent)"
        strokeWidth="3"
        strokeLinecap="round"
        className="animate-[spinner-dash_1.5s_ease-in-out_infinite]"
      />
    </svg>
  );
}

// ── Terminal Log Component ───────────────────────────────────
function TerminalLog({
  logs,
  logEndRef,
  variant = "default",
  eventCount,
}: {
  logs: string[];
  logEndRef?: React.RefObject<HTMLDivElement | null>;
  variant?: "default" | "error";
  eventCount?: number;
}) {
  const dotColor = variant === "error" ? "bg-error" : "bg-accent";
  const count = eventCount ?? logs.length;

  return (
    <div className="w-full rounded-lg border border-neutral-800 bg-surface-1/50 overflow-hidden">
      {/* Terminal chrome */}
      <div className="flex items-center gap-2 px-4 py-2 border-b border-neutral-800 bg-surface-1">
        <div className="flex items-center gap-1.5">
          <div className={`w-2 h-2 rounded-full ${dotColor}`} />
          <div className="w-2 h-2 rounded-full bg-neutral-700" />
          <div className="w-2 h-2 rounded-full bg-neutral-700" />
        </div>
        <span className="text-[10px] font-mono uppercase tracking-widest text-neutral-500 ml-2">Activity</span>
        <span className="text-[10px] font-mono text-neutral-600 ml-auto">{count} events</span>
      </div>
      <div className="max-h-48 overflow-y-auto p-3 space-y-0.5">
        {logs.map((log, i) => (
          <p key={i} className="text-xs font-mono leading-relaxed text-neutral-500">
            <span className="text-neutral-700 select-none mr-2">{String(i + 1).padStart(2, "0")}</span>
            {log}
          </p>
        ))}
        {/* Blinking cursor on last line */}
        {variant !== "error" && (
          <span className="inline-block w-1.5 h-3.5 bg-accent animate-[cursor-blink_1s_step-end_infinite] align-middle ml-1" />
        )}
        {logEndRef && <div ref={logEndRef} />}
      </div>
    </div>
  );
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
  const [staticHtml, setStaticHtml] = useState("");
  const logEndRef = useRef<HTMLDivElement>(null);
  const [showLogs, setShowLogs] = useState(true);
  const [elapsed, setElapsed] = useState(0);
  const phaseStartRef = useRef<number>(Date.now());

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

      function processSSELine(line: string) {
        if (!line.startsWith("data: ")) return;
        const payload = JSON.parse(line.slice(6));

        // Log events
        if (payload.log) {
          setLogs((prev) => [...prev, payload.log]);
        }

        if (payload.status === "error") {
          throw new Error(payload.message || "Something went wrong");
        }

        if (payload.status === "done") {
          const receivedFiles = payload.files || {};
          // Use payload.code, or fall back to page.tsx from files
          const pageCode = payload.code || receivedFiles["src/app/page.tsx"] || "";
          setCode(pageCode);
          setFiles(receivedFiles);
          if (payload.preview_url) setPreviewUrl(payload.preview_url);
          if (payload.clone_id) setCloneId(payload.clone_id);
          if (payload.static_html) setStaticHtml(payload.static_html);
          setPhase("done");
          // Refresh history
          fetch(`${process.env.NEXT_PUBLIC_API_URL}/clones`)
            .then((r) => r.json())
            .then((data) => { if (data.clones) setHistory(data.clones); })
            .catch(() => {});
        } else if (payload.status) {
          if (payload.message) {
            setLogs((prev) => [...prev, `▸ ${payload.message}`]);
          }
          setPhase(payload.status);
          if (payload.message) setStatusMessage(payload.message);
        }

        if (payload.type === "file_write") {
          setLogs((prev) => [...prev, `  + ${payload.file} (${payload.lines} lines)`]);
        }
      }

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";

        for (const line of lines) {
          processSSELine(line);
        }
      }

      // Flush remaining buffer — the "done" event may still be here
      // if the stream closed before the trailing newline arrived
      buffer += decoder.decode(); // flush decoder
      if (buffer.trim()) {
        for (const line of buffer.split("\n")) {
          processSSELine(line);
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
    setStaticHtml("");
    setShowLogs(true);
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

  // Running elapsed timer during loading phases
  useEffect(() => {
    if (!isLoading) {
      setElapsed(0);
      return;
    }
    phaseStartRef.current = Date.now();
    setElapsed(0);
    const interval = setInterval(() => {
      setElapsed(Math.floor((Date.now() - phaseStartRef.current) / 1000));
    }, 1000);
    return () => clearInterval(interval);
  }, [isLoading]);

  // If selected file doesn't exist in files, fall back to first available TSX file
  const effectiveSelectedFile = files[selectedFile]
    ? selectedFile
    : Object.keys(files).find((f) => f.endsWith("page.tsx")) || Object.keys(files)[0] || selectedFile;
  const selectedFileContent = files[effectiveSelectedFile] || "";

  // ────────── Result view (Done state) ──────────
  if (phase === "done" && (code || Object.keys(files).length > 0)) {
    return (
      <div className="flex h-screen bg-surface-0">
        {/* Left Panel — Build Log */}
        {showLogs && logs.length > 0 && (
          <div className="w-80 shrink-0 flex flex-col border-r border-neutral-800/80 bg-surface-0">
            {/* Panel header */}
            <div className="flex items-center justify-between px-4 py-2.5 border-b border-neutral-800 bg-surface-1/80">
              <div className="flex items-center gap-2">
                <div className="flex items-center gap-1.5">
                  <div className="w-2 h-2 rounded-full bg-emerald-500 shadow-[0_0_6px_rgba(16,185,129,0.4)]" />
                  <div className="w-2 h-2 rounded-full bg-neutral-700" />
                  <div className="w-2 h-2 rounded-full bg-neutral-700" />
                </div>
                <span className="text-[10px] font-mono uppercase tracking-widest text-neutral-500 ml-1">Build Log</span>
              </div>
              <div className="flex items-center gap-2">
                <span className="text-[10px] font-mono text-neutral-600">{logs.length} events</span>
                <button
                  onClick={() => setShowLogs(false)}
                  className="p-1 rounded hover:bg-surface-2 text-neutral-600 hover:text-neutral-400 transition-colors"
                  title="Collapse log panel"
                >
                  <svg className="w-3.5 h-3.5" viewBox="0 0 20 20" fill="currentColor">
                    <path fillRule="evenodd" d="M12.707 5.293a1 1 0 010 1.414L9.414 10l3.293 3.293a1 1 0 01-1.414 1.414l-4-4a1 1 0 010-1.414l4-4a1 1 0 011.414 0z" clipRule="evenodd" />
                  </svg>
                </button>
              </div>
            </div>

            {/* Scrollable log body */}
            <div className="flex-1 overflow-y-auto log-scroll py-2 px-3 space-y-px">
              {logs.map((log, i) => {
                const isFileWrite = log.startsWith("  +");
                const isStatus = log.startsWith("\u25b8");
                return (
                  <p
                    key={i}
                    className={`text-[11px] font-mono leading-relaxed py-px ${
                      isFileWrite ? "text-accent/70" :
                      isStatus ? "text-neutral-400" :
                      "text-neutral-500"
                    }`}
                  >
                    <span className="text-neutral-700/50 select-none mr-2 text-[10px]">
                      {String(i + 1).padStart(3, "0")}
                    </span>
                    {log}
                  </p>
                );
              })}
            </div>

            {/* Completion footer */}
            <div className="px-4 py-2.5 border-t border-neutral-800 bg-surface-1/40">
              <div className="flex items-center gap-2">
                <svg className="w-3.5 h-3.5 text-emerald-500" viewBox="0 0 20 20" fill="currentColor">
                  <path fillRule="evenodd" d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z" clipRule="evenodd" />
                </svg>
                <span className="text-[10px] font-mono uppercase tracking-widest text-emerald-500/80">Clone complete</span>
              </div>
            </div>
          </div>
        )}

        {/* Right Panel — Preview / Code */}
        <div className="flex-1 flex flex-col min-w-0">
          {/* Top bar */}
          <div className="flex items-center gap-3 px-5 py-3 border-b border-neutral-800 bg-surface-0">
            {!showLogs && logs.length > 0 && (
              <button
                onClick={() => setShowLogs(true)}
                className="p-1.5 rounded-md hover:bg-surface-2 text-neutral-500 hover:text-neutral-300 transition-colors"
                title="Show build log"
              >
                <svg className="w-4 h-4" viewBox="0 0 20 20" fill="currentColor">
                  <path fillRule="evenodd" d="M3 5a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1zM3 10a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1zM3 15a1 1 0 011-1h6a1 1 0 110 2H4a1 1 0 01-1-1z" clipRule="evenodd" />
                </svg>
              </button>
            )}
            <button
              onClick={handleReset}
              className="flex items-center gap-2 hover:opacity-80 transition-opacity"
            >
              <svg width="24" height="24" viewBox="0 0 173 173" fill="none" xmlns="http://www.w3.org/2000/svg">
                <rect width="172.339" height="172.339" rx="10" fill="black"/>
                <rect x="79" y="36" width="20" height="7.5" rx="0.96" fill="#FFFEFE"/>
                <rect x="72" y="49" width="20" height="7.5" rx="0.96" fill="#FFFEFE"/>
                <rect x="65" y="63" width="33" height="7.5" rx="0.96" fill="#FFFEFE"/>
                <rect x="59" y="76" width="19" height="7.5" rx="0.96" fill="#FFFEFE"/>
                <rect x="51" y="90" width="60" height="7.5" rx="0.96" fill="#FFFEFE"/>
                <rect x="45" y="104" width="20" height="7.5" rx="0.96" fill="#FFFEFE"/>
                <rect x="110" y="104" width="18" height="7.5" rx="0.96" fill="#FFFEFE"/>
                <rect x="40" y="118" width="20" height="7.5" rx="0.96" fill="#FFFEFE"/>
                <rect x="115" y="118" width="21" height="7.5" rx="0.96" fill="#FFFEFE"/>
                <rect x="23" y="131" width="45" height="7.5" rx="0.96" fill="#FFFEFE"/>
                <rect x="109" y="131" width="40" height="7.5" rx="0.96" fill="#FFFEFE"/>
              </svg>
              <span className="text-sm font-serif font-semibold tracking-tight text-neutral-50">Clone</span>
            </button>
            <div className="h-4 w-px bg-neutral-700" />

            {/* Preview / Code toggle */}
            <div className="flex rounded-md bg-surface-1 p-0.5">
              <button
                onClick={() => setTab("preview")}
                className={`text-xs font-mono uppercase tracking-wider px-3 py-1 rounded transition-colors ${
                  tab === "preview"
                    ? "bg-accent text-surface-0"
                    : "text-neutral-400 hover:text-neutral-200"
                }`}
              >
                Preview
              </button>
              <button
                onClick={() => setTab("code")}
                className={`text-xs font-mono uppercase tracking-wider px-3 py-1 rounded transition-colors ${
                  tab === "code"
                    ? "bg-accent text-surface-0"
                    : "text-neutral-400 hover:text-neutral-200"
                }`}
              >
                Code
              </button>
            </div>

            <span className="text-sm text-neutral-500 font-mono truncate flex-1">{url}</span>
            <button
              onClick={handleReset}
              className="text-xs font-mono uppercase tracking-wider px-3 py-1.5 rounded-md border border-neutral-700 text-neutral-400 hover:border-accent hover:text-accent transition-colors"
            >
              Clone another
            </button>
          </div>

          {/* Preview — live sandbox iframe, static HTML preview, or redeploy prompt */}
          {previewUrl ? (
            <iframe
              src={previewUrl}
              sandbox="allow-scripts allow-same-origin"
              className={`flex-1 w-full border-none bg-white ${tab !== "preview" ? "hidden" : ""}`}
              title="Cloned website"
            />
          ) : staticHtml ? (
            <div className={`flex-1 flex flex-col w-full ${tab !== "preview" ? "hidden" : ""}`}>
              <iframe
                srcDoc={staticHtml}
                sandbox="allow-scripts allow-same-origin"
                className="flex-1 w-full border-none bg-white"
                title="Static preview"
              />
              {cloneId && (
                <div className="flex items-center gap-3 px-4 py-2 bg-surface-1 border-t border-neutral-800">
                  <span className="text-xs font-mono text-neutral-500">Static preview</span>
                  <button
                    onClick={handleRedeploy}
                    disabled={redeploying}
                    className="text-xs font-mono text-accent hover:underline disabled:opacity-50"
                  >
                    {redeploying ? "Deploying..." : "Launch live sandbox"}
                  </button>
                  {redeploying && redeployLogs.length > 0 && (
                    <span className="text-xs font-mono text-neutral-500 truncate flex-1">
                      {redeployLogs[redeployLogs.length - 1]}
                    </span>
                  )}
                </div>
              )}
            </div>
          ) : (
            tab === "preview" && (
              <div className="flex-1 flex items-center justify-center bg-surface-0">
                <div className="text-center space-y-4">
                  {redeploying ? (
                    <div className="w-full max-w-2xl mx-auto flex flex-col items-center gap-6 px-4">
                      <div className="flex flex-col items-center gap-3">
                        <ArcSpinner />
                        <p className="text-sm font-mono text-neutral-50">Re-deploying to sandbox...</p>
                      </div>
                      {redeployLogs.length > 0 && (
                        <TerminalLog logs={redeployLogs} />
                      )}
                    </div>
                  ) : (
                    <>
                      <p className="text-neutral-400 text-sm font-mono">
                        No preview available.
                      </p>
                      {cloneId && (
                        <button
                          onClick={handleRedeploy}
                          className="px-4 py-2 rounded-lg bg-accent text-surface-0 text-sm font-mono font-semibold hover:brightness-110 transition-all"
                        >
                          Deploy to sandbox
                        </button>
                      )}
                      <p className="text-neutral-500 text-xs font-mono">
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
            <div className="flex-1 flex overflow-hidden bg-surface-0">
              {/* File tree sidebar */}
              <div className="w-60 shrink-0 border-r border-neutral-800 flex flex-col overflow-hidden">
                <div className="px-3 py-2 border-b border-neutral-800 bg-surface-1/50">
                  <span className="text-[10px] font-mono uppercase tracking-widest text-neutral-500">Files</span>
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
                <div className="flex items-center justify-between px-5 py-2 border-b border-neutral-800">
                  <span className="text-xs text-neutral-500 font-mono">{effectiveSelectedFile}</span>
                  <button
                    onClick={() => {
                      navigator.clipboard.writeText(selectedFileContent);
                    }}
                    className="text-xs font-mono px-2 py-1 rounded bg-surface-2 text-neutral-400 hover:text-accent hover:bg-surface-3 transition-colors"
                  >
                    Copy
                  </button>
                </div>
                <div className="flex-1 overflow-auto">
                  <Highlight theme={themes.nightOwl} code={selectedFileContent} language={getLang(effectiveSelectedFile)}>
                    {({ style, tokens, getLineProps, getTokenProps }) => (
                      <pre style={{ ...style, margin: 0, padding: "1.25rem", background: "transparent" }} className="text-xs font-mono leading-relaxed">
                        {tokens.map((line, i) => (
                          <div key={i} {...getLineProps({ line })} className="table-row">
                            <span className="table-cell pr-4 select-none text-right text-neutral-600 w-10">{i + 1}</span>
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
      </div>
    );
  }

  // ────────── Landing / Loading / Error view ──────────
  return (
    <div className="relative flex flex-col items-center justify-center min-h-screen px-6 overflow-hidden">
      {/* Hero glow */}
      <div className="hero-glow absolute inset-0 pointer-events-none" />

      {/* Hero */}
      <div className="relative flex flex-col items-center gap-5 mb-10 text-center">
        {/* Logo — bars pulse blue when loading */}
        <div className="relative animate-fade-in-up">
          <div className="absolute inset-0 blur-2xl bg-accent/10 rounded-full scale-150" />
          <svg width="72" height="72" viewBox="0 0 173 173" fill="none" xmlns="http://www.w3.org/2000/svg" className={`relative ${isLoading ? "animate-[logo-pulse_2s_ease-in-out_infinite]" : ""}`}>
            <rect width="172.339" height="172.339" rx="10" fill="black"/>
            {[
              { x: 79, y: 36, w: 20 },
              { x: 72, y: 49, w: 20 },
              { x: 65, y: 63, w: 33 },
              { x: 59, y: 76, w: 19 },
              { x: 51, y: 90, w: 60 },
              { x: 45, y: 104, w: 20 },
              { x: 110, y: 104, w: 18 },
              { x: 40, y: 118, w: 20 },
              { x: 115, y: 118, w: 21 },
              { x: 23, y: 131, w: 45 },
              { x: 109, y: 131, w: 40 },
            ].map((r, i) => (
              <rect key={i} x={r.x} y={r.y} width={r.w} height={7.5} rx={0.96} fill={isLoading ? "#3B82F6" : "#FFFEFE"}>
                {isLoading && (
                  <animate attributeName="opacity" values="1;0.3;1" dur="2s" begin={`${i * 0.1}s`} repeatCount="indefinite" />
                )}
              </rect>
            ))}
          </svg>
        </div>

        <h1 className="font-serif text-7xl font-bold tracking-tight text-neutral-50 animate-fade-in-up delay-100">
          Clone
        </h1>
        <p className="text-sm font-mono uppercase tracking-[0.2em] text-neutral-400 max-w-md animate-fade-in-up delay-200">
          Paste any URL. Get an AI-generated replica in minutes.
        </p>
      </div>

      {/* URL Input — button embedded inside */}
      <form
        onSubmit={handleSubmit}
        className="relative w-full max-w-xl animate-fade-in-up delay-300"
      >
        <div className="relative input-glow rounded-xl border border-neutral-700 bg-surface-1 transition-all">
          <input
            type="url"
            required
            placeholder="https://example.com"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            disabled={isLoading}
            className="w-full h-14 pl-4 pr-32 rounded-xl bg-transparent text-neutral-50 font-mono text-ellipsis placeholder:text-neutral-600 focus:outline-none disabled:opacity-50 transition-all"
          />
          <button
            type="submit"
            disabled={isLoading}
            className="absolute right-2 top-1/2 -translate-y-1/2 h-10 px-5 rounded-lg bg-accent text-surface-0 font-mono font-semibold text-sm whitespace-nowrap shrink-0 hover:brightness-110 disabled:opacity-50 transition-all"
          >
            {isLoading ? "Cloning..." : "Clone"}
          </button>
        </div>
      </form>

      {/* Progress + Activity Log */}
      {isLoading && (
        <div className="mt-8 w-full max-w-2xl flex flex-col items-center gap-6 animate-fade-in-up">
          {/* Step indicator */}
          <div className="flex flex-col items-center gap-4">
            <div className="flex items-center gap-4">
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
                  <div key={step.key} className="flex items-center gap-4">
                    {i > 0 && (
                      <div className={`w-8 h-px ${isCompleted || isActive ? "bg-accent" : "bg-neutral-700"}`} />
                    )}
                    <div className="flex items-center gap-2">
                      <div className={`w-2 h-2 rounded-full transition-colors ${
                        isActive
                          ? "bg-accent animate-[glow-pulse_2s_ease-in-out_infinite]"
                          : isCompleted
                          ? "bg-accent"
                          : "bg-neutral-700"
                      }`} />
                      <span className={`text-[10px] font-mono uppercase tracking-widest transition-colors ${
                        isActive ? "text-accent" : isCompleted ? "text-neutral-400" : "text-neutral-600"
                      }`}>
                        {step.label}
                      </span>
                    </div>
                  </div>
                );
              })}
            </div>

            <p className="text-sm font-mono text-neutral-300">
              {statusMessage || (
                phase === "scraping" ? "Scraping website..." :
                phase === "generating" ? "Generating clone with AI..." :
                phase === "fixing" ? "Fixing build errors..." :
                "Deploying to sandbox..."
              )}
              {elapsed > 0 && (
                <span className="text-neutral-600 ml-2">{elapsed}s</span>
              )}
            </p>
          </div>

          {/* Activity Log */}
          {logs.length > 0 && (
            <TerminalLog logs={logs} logEndRef={logEndRef} />
          )}
        </div>
      )}

      {/* Error */}
      {phase === "error" && (
        <div className="mt-6 w-full max-w-2xl space-y-3 animate-fade-in-up">
          {error && (
            <div className="px-4 py-3 rounded-lg bg-error-dim border border-error/20">
              <p className="text-sm font-mono text-error">{error}</p>
            </div>
          )}

          {/* Show activity log on error too */}
          {logs.length > 0 && (
            <TerminalLog logs={logs} variant="error" />
          )}

          <button
            onClick={handleReset}
            className="text-sm font-mono text-neutral-400 hover:text-accent hover:underline underline-offset-4 transition-colors"
          >
            Try again
          </button>
        </div>
      )}

      {/* Clone History */}
      {!isLoading && phase !== "error" && history.length > 0 && (
        <div className="relative mt-12 w-full max-w-2xl animate-fade-in-up delay-400">
          <h2 className="text-[10px] font-mono uppercase tracking-[0.2em] text-neutral-500 mb-3">Recent clones</h2>
          <div className="space-y-2">
            {history.map((clone) => (
              <div
                key={clone.id}
                className={`flex items-center gap-3 px-4 py-3 rounded-lg border border-neutral-800 bg-surface-1/50 transition-colors group ${
                  clone.status === "done" ? "hover:border-neutral-600" : "opacity-60"
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
                        setCloneId(data.id);
                        setTab("preview");
                        setPhase("done");

                        // Use static HTML if available (instant), otherwise redeploy
                        if (data.static_html) {
                          setStaticHtml(data.static_html);
                          setPreviewUrl("");
                        } else {
                          setStaticHtml("");
                          setPreviewUrl("");
                          setRedeploying(true);
                          setRedeployLogs([]);
                          fetch(`${process.env.NEXT_PUBLIC_API_URL}/clones/${data.id}/redeploy`, { method: "POST" })
                            .then(async (res) => {
                              if (!res.ok) throw new Error(`Request failed (${res.status})`);
                              const reader = res.body?.getReader();
                              if (!reader) throw new Error("No response body");
                              const decoder = new TextDecoder();
                              let buf = "";
                              while (true) {
                                const { done, value } = await reader.read();
                                if (done) break;
                                buf += decoder.decode(value, { stream: true });
                                const lines = buf.split("\n");
                                buf = lines.pop() || "";
                                for (const line of lines) {
                                  if (!line.startsWith("data: ")) continue;
                                  const payload = JSON.parse(line.slice(6));
                                  if (payload.log) setRedeployLogs((prev) => [...prev, payload.log]);
                                  if (payload.status === "done" && payload.preview_url) {
                                    setPreviewUrl(payload.preview_url);
                                  }
                                  if (payload.status === "error") {
                                    setRedeployLogs((prev) => [...prev, `Error: ${payload.message}`]);
                                  }
                                }
                              }
                            })
                            .catch((err: unknown) => {
                              setRedeployLogs((prev) => [...prev, `Error: ${err instanceof Error ? err.message : "Failed"}`]);
                            })
                            .finally(() => setRedeploying(false));
                        }
                      })
                      .catch(() => {});
                  }}
                  className={`flex items-center gap-3 flex-1 min-w-0 text-left ${
                    clone.status === "done" ? "cursor-pointer" : "cursor-not-allowed"
                  }`}
                >
                  <div className={`w-2 h-2 rounded-full shrink-0 ${
                    clone.status === "done" ? "bg-accent" :
                    clone.status === "error" ? "bg-error" :
                    "bg-yellow-500 animate-pulse"
                  }`} />
                  <span className="text-sm font-mono text-neutral-300 truncate flex-1">{clone.url}</span>
                  <span className="text-xs font-mono text-neutral-600 shrink-0">
                    {new Date(clone.created_at).toLocaleDateString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}
                  </span>
                </button>
                <button
                  onClick={() => {
                    fetch(`${process.env.NEXT_PUBLIC_API_URL}/clones/${clone.id}`, { method: "DELETE" })
                      .then(() => setHistory((prev) => prev.filter((c) => c.id !== clone.id)))
                      .catch(() => {});
                  }}
                  className="opacity-0 group-hover:opacity-100 text-neutral-600 hover:text-error transition-all shrink-0 p-1"
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
