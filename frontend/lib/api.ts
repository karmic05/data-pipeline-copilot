/**
 * API client for the FastAPI backend. All LLM text arrives via streamTask(),
 * which reads an SSE-formatted fetch stream (`data: {"token": "..."}` events).
 */
import type {
  AgentRun,
  AgentRunRequest,
  AnalysisReport,
  AnalyzeRequest,
  ConnectorConfig,
  ConnectorInfo,
  ConnectorTestResponse,
  CostAnalysis,
  ExplainRequest,
  GenerateRequest,
  GenerateResponse,
  HealthResponse,
  IntrospectResponse,
  ProviderInfo,
  SimulateRequest,
  SimulateResponse,
} from "./types";

// Strip any UTF-8 BOM (U+FEFF) and surrounding whitespace, which some shells
// (e.g. PowerShell piping) inject when setting the env var, plus any trailing
// slash so request paths join cleanly.
const BOM_RE = new RegExp(String.fromCharCode(0xfeff), "g");
export const API_BASE = (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000")
  .replace(BOM_RE, "")
  .trim()
  .replace(/\/+$/, "");

class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await res
      .json()
      .then((j) => j.detail ?? JSON.stringify(j))
      .catch(() => res.statusText);
    throw new ApiError(String(detail), res.status);
  }
  return res.json() as Promise<T>;
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`);
  if (!res.ok) throw new ApiError(res.statusText, res.status);
  return res.json() as Promise<T>;
}

export function analyze(req: AnalyzeRequest): Promise<AnalysisReport> {
  return post<AnalysisReport>("/api/analyze", req);
}

export function simulateImpact(req: SimulateRequest): Promise<SimulateResponse> {
  return post<SimulateResponse>("/api/simulate/impact", req);
}

export function estimateCost(req: SimulateRequest): Promise<CostAnalysis> {
  return post<CostAnalysis>("/api/cost/estimate", req);
}

export function generatePipeline(
  req: GenerateRequest,
): Promise<GenerateResponse> {
  return post<GenerateResponse>("/api/generate", req);
}

export function runAgent(req: AgentRunRequest): Promise<AgentRun> {
  return post<AgentRun>("/api/agent/run", req);
}

export function getHealth(): Promise<HealthResponse> {
  return get<HealthResponse>("/api/health");
}

export function getProviders(): Promise<ProviderInfo[]> {
  return get<ProviderInfo[]>("/api/providers");
}

// ── Phase 2: live database connectors ───────────────────────────────────────

export function getConnectors(): Promise<ConnectorInfo[]> {
  return get<ConnectorInfo[]>("/api/connectors");
}

export function testConnector(
  config: ConnectorConfig,
): Promise<ConnectorTestResponse> {
  return post<ConnectorTestResponse>("/api/connectors/test", { config });
}

export function introspectConnector(
  config: ConnectorConfig,
  table?: string,
): Promise<IntrospectResponse> {
  return post<IntrospectResponse>("/api/connectors/introspect", {
    config,
    table,
  });
}

export interface StreamHandlers {
  onToken: (token: string) => void;
  onDone?: () => void;
  onError?: (message: string) => void;
}

/**
 * Stream an LLM task over SSE. Returns an abort function.
 * Events: `data: {"token": "..."}`, `data: {"done": true}`,
 * `data: {"error": "..."}`.
 */
export function streamTask(
  req: ExplainRequest,
  handlers: StreamHandlers,
): () => void {
  const controller = new AbortController();

  (async () => {
    try {
      const res = await fetch(`${API_BASE}/api/explain`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(req),
        signal: controller.signal,
      });
      if (!res.ok || !res.body) {
        handlers.onError?.(`Stream failed (${res.status})`);
        return;
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const events = buffer.split("\n\n");
        buffer = events.pop() ?? "";
        for (const event of events) {
          for (const line of event.split("\n")) {
            if (!line.startsWith("data:")) continue;
            const payload = line.slice(5).trim();
            if (!payload) continue;
            try {
              const msg = JSON.parse(payload) as {
                token?: string;
                done?: boolean;
                error?: string;
              };
              if (msg.token) handlers.onToken(msg.token);
              if (msg.error) handlers.onError?.(msg.error);
              if (msg.done) handlers.onDone?.();
            } catch {
              // tolerate non-JSON keepalive lines
            }
          }
        }
      }
      handlers.onDone?.();
    } catch (err) {
      if ((err as Error).name !== "AbortError") {
        handlers.onError?.((err as Error).message);
      }
    }
  })();

  return () => controller.abort();
}
