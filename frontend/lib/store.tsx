"use client";

/**
 * Shared analysis state. Wrap the page in <AnalysisProvider> and consume with
 * useAnalysis(). Tabs read `report`; the editor writes `code`; the top bar
 * calls `runAnalyze()`.
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { analyze, getHealth } from "./api";
import { DEFAULT_SAMPLE } from "./samples";
import type {
  AnalysisParams,
  AnalysisReport,
  ConnectorConfig,
  ProviderInfo,
  TabId,
} from "./types";

interface AnalysisState {
  code: string;
  setCode: (code: string) => void;
  report: AnalysisReport | null;
  analyzing: boolean;
  error: string | null;
  provider: ProviderInfo | null;
  params: AnalysisParams;
  setParams: (params: AnalysisParams) => void;
  dynamic: boolean;
  setDynamic: (v: boolean) => void;
  /** Active live DB connection — when set, analyses are grounded in it. */
  connection: ConnectorConfig | null;
  setConnection: (c: ConnectorConfig | null) => void;
  activeTab: TabId;
  setActiveTab: (tab: TabId) => void;
  runAnalyze: () => Promise<void>;
}

const AnalysisContext = createContext<AnalysisState | null>(null);

export function AnalysisProvider({ children }: { children: ReactNode }) {
  const [code, setCode] = useState<string>(DEFAULT_SAMPLE.code);
  const [report, setReport] = useState<AnalysisReport | null>(null);
  const [analyzing, setAnalyzing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [provider, setProvider] = useState<ProviderInfo | null>(null);
  const [activeTab, setActiveTab] = useState<TabId>("score");
  const [dynamic, setDynamic] = useState(false);
  const [connection, setConnection] = useState<ConnectorConfig | null>(null);
  const [params, setParams] = useState<AnalysisParams>({
    row_count: 10_000_000,
    daily_runs: 24,
    warehouse: "snowflake",
  });

  useEffect(() => {
    let cancelled = false;
    getHealth()
      .then((h) => {
        if (!cancelled) setProvider(h.provider);
      })
      .catch(() => {
        if (!cancelled) {
          setProvider({
            provider: "offline",
            model: "—",
            available: false,
            detail: "Backend unreachable at http://localhost:8000",
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const runAnalyze = useCallback(async () => {
    setAnalyzing(true);
    setError(null);
    try {
      const result = await analyze({
        code,
        format: "auto",
        row_count: params.row_count,
        daily_runs: params.daily_runs,
        warehouse: params.warehouse,
        dynamic,
        connection: connection ?? undefined,
      });
      setReport(result);
      setActiveTab("score");
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setAnalyzing(false);
    }
  }, [code, params, dynamic, connection]);

  const value = useMemo(
    () => ({
      code,
      setCode,
      report,
      analyzing,
      error,
      provider,
      params,
      setParams,
      dynamic,
      setDynamic,
      connection,
      setConnection,
      activeTab,
      setActiveTab,
      runAnalyze,
    }),
    [
      code,
      report,
      analyzing,
      error,
      provider,
      params,
      dynamic,
      connection,
      activeTab,
      runAnalyze,
    ],
  );

  return (
    <AnalysisContext.Provider value={value}>
      {children}
    </AnalysisContext.Provider>
  );
}

export function useAnalysis(): AnalysisState {
  const ctx = useContext(AnalysisContext);
  if (!ctx) throw new Error("useAnalysis must be used within AnalysisProvider");
  return ctx;
}
