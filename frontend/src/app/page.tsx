"use client";

import { useState, useRef, useEffect, FormEvent } from "react";

type Phase = "idle" | "scraping" | "generating" | "deploying" | "fixing" | "done" | "error";

export default function Home() {
  const [url, setUrl] = useState("");
  const [code, setCode] = useState("");
  const [previewUrl, setPreviewUrl] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [error, setError] = useState("");
  const [tab, setTab] = useState<"preview" | "code">("preview");
  const [statusMessage, setStatusMessage] = useState("");
  const [logs, setLogs] = useState<string[]>([]);
  const logEndRef = useRef<HTMLDivElement>(null);

  // Auto-scroll activity log to bottom
  useEffect(() => {
    logEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [logs]);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError("");
    setCode("");
    setPreviewUrl("");
    setStatusMessage("");
    setLogs([]);
    setPhase("scraping");

    try {
      const res = await fetch("http://localhost:8000/clone", {
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
            if (payload.preview_url) setPreviewUrl(payload.preview_url);
            setPhase("done");
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
    setPreviewUrl("");
    setError("");
    setUrl("");
    setTab("preview");
    setStatusMessage("");
    setLogs([]);
  }

  const isLoading = phase === "scraping" || phase === "generating" || phase === "deploying" || phase === "fixing";

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

        {/* Preview */}
        {tab === "preview" && (
          previewUrl ? (
            <iframe
              src={previewUrl}
              className="flex-1 w-full border-none bg-white"
              title="Cloned website"
            />
          ) : (
            <div className="flex-1 flex items-center justify-center bg-zinc-950">
              <div className="text-center">
                <p className="text-zinc-400 text-sm">
                  Preview requires Daytona sandbox deployment.
                </p>
                <p className="text-zinc-500 text-xs mt-1">
                  Switch to the Code tab to see the generated component.
                </p>
              </div>
            </div>
          )
        )}

        {/* Code view */}
        {tab === "code" && (
          <div className="flex-1 flex flex-col overflow-hidden bg-zinc-950">
            <div className="flex items-center justify-between px-5 py-2 border-b border-zinc-800">
              <span className="text-xs text-zinc-500 font-mono">page.tsx</span>
              <button
                onClick={() => {
                  navigator.clipboard.writeText(code);
                }}
                className="text-xs px-2 py-1 rounded bg-zinc-800 text-zinc-400 hover:text-white hover:bg-zinc-700 transition-colors"
              >
                Copy
              </button>
            </div>
            <div className="flex-1 overflow-auto p-5">
              <pre className="text-sm text-zinc-300 font-mono whitespace-pre-wrap break-words leading-relaxed">
                <code>{code}</code>
              </pre>
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
    </div>
  );
}
