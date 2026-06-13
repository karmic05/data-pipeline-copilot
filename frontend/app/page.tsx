"use client";

/**
 * Application shell. Wraps the whole experience in the analysis store and lays
 * out the top bar over a two-column split pane: source editor on the left,
 * analysis results on the right.
 */
import AnalysisPanel from "@/components/analysis-panel";
import EditorPanel from "@/components/editor-panel";
import TopBar from "@/components/top-bar";
import { AnalysisProvider } from "@/lib/store";

export default function Page() {
  return (
    <AnalysisProvider>
      <div className="flex h-dvh flex-col overflow-hidden bg-paper text-ink paper-texture">
        <TopBar />
        <main className="grid min-h-0 flex-1 grid-cols-1 lg:grid-cols-[minmax(380px,44%)_1fr]">
          <div className="min-h-0 min-w-0">
            <EditorPanel />
          </div>
          <div className="min-h-0 min-w-0">
            <AnalysisPanel />
          </div>
        </main>
      </div>
    </AnalysisProvider>
  );
}
