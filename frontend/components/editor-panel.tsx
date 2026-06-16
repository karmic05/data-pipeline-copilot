"use client";

/**
 * Left-hand editor pane: a Monaco editor wired to the shared `code` state plus
 * a sample picker and live line/char counters. Monaco is loaded dynamically
 * with SSR disabled so it never runs on the server.
 */
import { useMemo, useState } from "react";
import dynamic from "next/dynamic";
import { Wand2 } from "lucide-react";
import type { OnChange, OnMount, BeforeMount } from "@monaco-editor/react";

import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/select";
import { Tooltip } from "@/components/ui/tooltip";
import { generatePipeline } from "@/lib/api";
import { useAnalysis } from "@/lib/store";
import { samples } from "@/lib/samples";
import { cn } from "@/lib/utils";

const MonacoEditor = dynamic(
  () => import("@monaco-editor/react").then((m) => m.default),
  {
    ssr: false,
    loading: () => (
      <div className="flex h-full items-center justify-center bg-paper2 font-mono text-xs text-inksoft">
        loading editor…
      </div>
    ),
  },
);

const sampleOptions = samples.map((s) => ({ value: s.id, label: s.label }));

/** Derive a Monaco language id from the source heuristically. */
function detectLanguage(code: string): string {
  const head = code.trimStart();
  if (head.startsWith("import") || head.startsWith("from") || code.includes("\ndef ")) {
    return "python";
  }
  if (code.includes("version: 2") || code.includes("expectation_type")) {
    return "yaml";
  }
  return "sql";
}

export function EditorPanel() {
  const { code, setCode, analyzing, runAnalyze, dynamic, setDynamic } =
    useAnalysis();

  const [prompt, setPrompt] = useState("");
  const [generating, setGenerating] = useState(false);
  const [genError, setGenError] = useState<string | null>(null);
  const [genNotes, setGenNotes] = useState<string[]>([]);

  const generate = async () => {
    const trimmed = prompt.trim();
    if (!trimmed || generating) return;
    setGenerating(true);
    setGenError(null);
    setGenNotes([]);
    try {
      const result = await generatePipeline({
        prompt: trimmed,
        target_format: "auto",
      });
      setCode(result.code);
      setGenNotes(result.notes ?? []);
    } catch (err) {
      setGenError((err as Error).message);
    } finally {
      setGenerating(false);
    }
  };

  const language = useMemo(() => detectLanguage(code), [code]);

  const lineCount = useMemo(() => code.split("\n").length, [code]);
  const charCount = code.length;

  const samplePicker = (
    <Select
      value=""
      onChange={(id) => {
        const sample = samples.find((s) => s.id === id);
        if (sample) setCode(sample.code);
      }}
      options={sampleOptions}
      label="Load sample"
    />
  );

  const beforeMount: BeforeMount = (monaco) => {
    monaco.editor.defineTheme("paper", {
      base: "vs",
      inherit: true,
      colors: {
        "editor.background": "#FDFAF2",
        "editor.foreground": "#2E2620",
        "editor.lineHighlightBackground": "#EDE4D0",
        "editor.selectionBackground": "#1F8FB855",
      },
      rules: [
        { token: "keyword", foreground: "C13E2C", fontStyle: "bold" },
        { token: "string", foreground: "6F8F6A" },
        { token: "number", foreground: "B8860B" },
        { token: "comment", foreground: "9A8B76", fontStyle: "italic" },
      ],
    });
  };

  const onMount: OnMount = (editor, monaco) => {
    editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.Enter, () => {
      void runAnalyze();
    });
    monaco.editor.setTheme("paper");
  };

  const onChange: OnChange = (value) => {
    setCode(value ?? "");
  };

  return (
    <section className="flex h-full min-h-0 flex-col border-r-2 border-line">
      <div className="space-y-2.5 border-b border-line bg-paper2 px-4 py-3">
        <div className="flex items-center gap-2">
          <span className="text-[10px] uppercase tracking-widest text-inksoft">
            Generate from description
          </span>
          <Tooltip
            className="ml-auto"
            content="Adds advisory LLM-found issues on top of the deterministic rules."
          >
            <label
              className={cn(
                "inline-flex cursor-pointer select-none items-center gap-1.5 rounded-full border-2 border-ink px-3 py-1 text-xs font-medium transition-colors",
                dynamic
                  ? "bg-frost text-paper2"
                  : "bg-paper2 text-ink hover:bg-paper3",
              )}
            >
              <input
                type="checkbox"
                checked={dynamic}
                onChange={(e) => setDynamic(e.target.checked)}
                className="sr-only"
              />
              <span
                aria-hidden="true"
                className={cn(
                  "inline-block h-2 w-2 rounded-full",
                  dynamic ? "bg-paper2" : "bg-frost",
                )}
              />
              AI dynamic review
            </label>
          </Tooltip>
        </div>

        <div className="flex items-stretch gap-2">
          <input
            type="text"
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                void generate();
              }
            }}
            placeholder="Describe a pipeline - 'daily Snowflake job joining orders and customers with revenue per region'"
            className="min-w-0 flex-1 rounded-xl border-2 border-line bg-paper px-3 py-2 text-sm text-ink placeholder:text-inksoft focus:border-ink focus:outline-none"
          />
          <Button
            variant="primary"
            size="md"
            onClick={() => void generate()}
            loading={generating}
            disabled={!prompt.trim()}
          >
            {!generating && <Wand2 aria-hidden="true" className="h-4 w-4" />}
            Generate
          </Button>
        </div>

        {genError && (
          <p className="text-xs text-terra">Generation failed: {genError}</p>
        )}

        {genNotes.length > 0 && (
          <ul className="space-y-0.5 text-xs text-inksoft">
            {genNotes.map((note, i) => (
              <li key={i}>{note}</li>
            ))}
          </ul>
        )}
      </div>

      <div className="flex items-center gap-3 border-b border-line px-4 py-2">
        <span className="text-[10px] uppercase tracking-widest text-inksoft">
          Pipeline Source
        </span>
        {samplePicker}
        <span className="ml-auto font-mono text-xs text-inksoft">
          {lineCount} {lineCount === 1 ? "line" : "lines"} · {charCount} chars
        </span>
      </div>

      <div className="relative h-1 shrink-0 overflow-hidden">
        {analyzing ? (
          <div
            className="absolute inset-y-0 h-full w-1/3 bg-terra"
            style={{ animation: "shimmer 1.1s ease-in-out infinite" }}
            aria-hidden="true"
          />
        ) : null}
      </div>

      <div className="min-h-0 flex-1">
        <MonacoEditor
          height="100%"
          theme="paper"
          language={language}
          value={code}
          onChange={onChange}
          beforeMount={beforeMount}
          onMount={onMount}
          options={{
            fontFamily: "var(--font-mono), JetBrains Mono, monospace",
            fontSize: 13,
            minimap: { enabled: false },
            wordWrap: "on",
            scrollBeyondLastLine: false,
            padding: { top: 16 },
            renderLineHighlight: "all",
            smoothScrolling: true,
            automaticLayout: true,
          }}
        />
      </div>
    </section>
  );
}

export default EditorPanel;
