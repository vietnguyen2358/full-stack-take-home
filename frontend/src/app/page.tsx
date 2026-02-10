"use client";

import { useState, FormEvent } from "react";

type Phase = "idle" | "scraping" | "generating" | "done" | "error";

export default function Home() {
  const [url, setUrl] = useState("");
  const [html, setHtml] = useState("");
  const [previewUrl, setPreviewUrl] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [error, setError] = useState("");

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError("");
    setHtml("");
    setPhase("scraping");

    try {
      setPhase("scraping");
      // Small delay so the user sees the scraping step
      await new Promise((r) => setTimeout(r, 600));
      setPhase("generating");

      const res = await fetch("http://localhost:8000/clone", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url }),
      });

      if (!res.ok) {
        const data = await res.json().catch(() => null);
        throw new Error(data?.detail || `Request failed (${res.status})`);
      }

      const data = await res.json();
      setHtml(data.html);
      if (data.preview_url) setPreviewUrl(data.preview_url);
      setPhase("done");
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Something went wrong");
      setPhase("error");
    }
  }

  function handleReset() {
    setPhase("idle");
    setHtml("");
    setPreviewUrl("");
    setError("");
    setUrl("");
  }

  const isLoading = phase === "scraping" || phase === "generating";

  // ---------- Result view (after clone is done) ----------
  if (phase === "done" && html) {
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
          <span className="text-sm text-zinc-500 truncate flex-1">{url}</span>
          <button
            onClick={handleReset}
            className="text-sm px-3 py-1.5 rounded-md bg-zinc-800 text-zinc-300 hover:bg-zinc-700 hover:text-white transition-colors"
          >
            Clone another
          </button>
        </div>

        {/* Preview iframe — Daytona sandbox URL if available, else srcdoc fallback */}
        {previewUrl ? (
          <iframe
            src={previewUrl}
            className="flex-1 w-full border-none bg-white"
            title="Cloned website"
          />
        ) : (
          <iframe
            srcDoc={html}
            sandbox="allow-scripts"
            className="flex-1 w-full border-none bg-white"
            title="Cloned website"
          />
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

      {/* Progress indicator */}
      {isLoading && (
        <div className="mt-8 flex flex-col items-center gap-4">
          {/* Spinner */}
          <div className="w-8 h-8 border-2 border-zinc-700 border-t-white rounded-full" style={{ animation: "spin-slow 1s linear infinite" }} />
          <div className="flex flex-col items-center gap-1">
            <p className="text-sm font-medium text-white">
              {phase === "scraping" ? "Scraping website..." : "Generating clone with AI..."}
            </p>
            <p className="text-xs text-zinc-500">
              {phase === "scraping"
                ? "Taking a screenshot and extracting HTML"
                : "This may take up to a minute"}
            </p>
          </div>
        </div>
      )}

      {/* Error */}
      {phase === "error" && error && (
        <div className="mt-6 w-full max-w-xl">
          <div className="px-4 py-3 rounded-lg bg-red-950/50 border border-red-900/50">
            <p className="text-sm text-red-400">{error}</p>
          </div>
          <button
            onClick={handleReset}
            className="mt-3 text-sm text-zinc-400 hover:text-white transition-colors"
          >
            Try again
          </button>
        </div>
      )}
    </div>
  );
}
