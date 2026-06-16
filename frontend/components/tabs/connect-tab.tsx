"use client";

/**
 * Connect tab - Phase 2 UI for connecting a live database, testing it, and
 * browsing real schemas. Self-contained: it talks to the connector API
 * directly and does not depend on the global analysis report, so it renders
 * even before any pipeline has been analyzed.
 *
 * The DuckDB "demo" connector needs no credentials and works instantly. The
 * external connectors (postgres / snowflake / bigquery) may be disabled on the
 * public deployment (gated for safety) - disabled cards are not selectable and
 * surface the gating note.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { Database, Loader2, Plug, Search, Sparkles, Table2 } from "lucide-react";
import {
  getConnectors,
  introspectConnector,
  testConnector,
} from "@/lib/api";
import type {
  ColumnModel,
  ConnectorConfig,
  ConnectorInfo,
  ConnectorKind,
  ConnectorTestResponse,
  TableSchemaModel,
} from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge, type BadgeTone } from "@/components/ui/badge";
import { ShapeBurst } from "@/components/memphis";
import { useAnalysis } from "@/lib/store";
import { cn } from "@/lib/utils";

// ── Connector readiness toning ───────────────────────────────────────────────

type Readiness = "ready" | "disabled" | "missing";

function readinessOf(info: ConnectorInfo): Readiness {
  if (!info.available) return "missing";
  if (!info.enabled) return "disabled";
  return "ready";
}

const READINESS_TONE: Record<Readiness, BadgeTone> = {
  ready: "sage",
  disabled: "ochre",
  missing: "ink",
};

const READINESS_LABEL: Record<Readiness, string> = {
  ready: "ready",
  disabled: "disabled here",
  missing: "driver missing",
};

// ── Config field plumbing ────────────────────────────────────────────────────

interface FieldDef {
  key: keyof ConnectorFormState;
  label: string;
  placeholder?: string;
  password?: boolean;
}

/** Plain text state for every credential input across all kinds. */
interface ConnectorFormState {
  dsn: string;
  account: string;
  user: string;
  password: string;
  warehouse: string;
  database: string;
  schema_name: string;
  project: string;
}

const EMPTY_FORM: ConnectorFormState = {
  dsn: "",
  account: "",
  user: "",
  password: "",
  warehouse: "",
  database: "",
  schema_name: "",
  project: "",
};

const FIELDS_BY_KIND: Record<ConnectorKind, FieldDef[]> = {
  duckdb: [],
  postgres: [
    {
      key: "dsn",
      label: "Connection string (DSN)",
      placeholder: "postgresql://user:pass@host:5432/db",
    },
  ],
  snowflake: [
    { key: "account", label: "Account", placeholder: "xy12345.us-east-1" },
    { key: "user", label: "User", placeholder: "analytics_ro" },
    { key: "password", label: "Password", password: true },
    { key: "warehouse", label: "Warehouse", placeholder: "COMPUTE_WH" },
    { key: "database", label: "Database", placeholder: "ANALYTICS" },
    { key: "schema_name", label: "Schema", placeholder: "PUBLIC" },
  ],
  bigquery: [
    { key: "project", label: "Project", placeholder: "my-gcp-project" },
  ],
};

/** Build the API ConnectorConfig payload for a kind from the flat form state. */
function buildConfig(
  kind: ConnectorKind,
  form: ConnectorFormState,
): ConnectorConfig {
  const trimmed = (v: string): string | null => {
    const t = v.trim();
    return t.length ? t : null;
  };
  return {
    kind,
    dsn: trimmed(form.dsn),
    account: trimmed(form.account),
    user: trimmed(form.user),
    password: trimmed(form.password),
    warehouse: trimmed(form.warehouse),
    database: trimmed(form.database),
    schema_name: trimmed(form.schema_name),
    project: trimmed(form.project),
  };
}

// ── Small presentational helpers ─────────────────────────────────────────────

function Eyebrow({ children }: { children: React.ReactNode }) {
  return (
    <span className="inline-flex items-center gap-2 text-xs font-semibold uppercase tracking-widest text-inksoft">
      <Plug aria-hidden="true" className="h-3.5 w-3.5 text-frost" />
      {children}
    </span>
  );
}

function FieldLabel({
  htmlFor,
  children,
}: {
  htmlFor: string;
  children: React.ReactNode;
}) {
  return (
    <label
      htmlFor={htmlFor}
      className="text-xs font-medium uppercase tracking-wide text-inksoft"
    >
      {children}
    </label>
  );
}

const INPUT_CLASS =
  "w-full rounded-xl border-2 border-ink bg-paper2 px-3 py-2 text-sm font-medium text-ink shadow-block-sm transition placeholder:text-inksoft/60 hover:bg-paper3/60 focus:outline-none focus:ring-2 focus:ring-frost/40";

const fmtInt = (v: number): string =>
  new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 }).format(v);

// ── Connector option card ────────────────────────────────────────────────────

function ConnectorCard({
  info,
  selected,
  onSelect,
}: {
  info: ConnectorInfo;
  selected: boolean;
  onSelect: () => void;
}) {
  const readiness = readinessOf(info);
  const selectable = info.available && info.enabled;

  return (
    <button
      type="button"
      onClick={selectable ? onSelect : undefined}
      aria-pressed={selected}
      disabled={!selectable}
      className={cn(
        "flex h-full min-w-[15rem] flex-1 flex-col items-start gap-2 rounded-2xl border-2 p-4 text-left transition",
        selectable
          ? "cursor-pointer border-ink bg-paper2 shadow-block-sm hover:bg-paper3/60"
          : "cursor-not-allowed border-line bg-paper3/40",
        selected && "bg-frost/10 ring-2 ring-frost",
      )}
    >
      <div className="flex w-full items-center justify-between gap-2">
        <span className="flex items-center gap-2 font-display text-lg text-ink">
          <Database aria-hidden="true" className="h-4 w-4 text-frost" />
          {info.label}
        </span>
        <Badge tone={READINESS_TONE[readiness]}>
          {READINESS_LABEL[readiness]}
        </Badge>
      </div>
      <p className="text-sm leading-relaxed text-inksoft">{info.detail}</p>
      {!info.requires_credentials && (
        <span className="inline-flex items-center rounded-full border border-sage/45 bg-sage/15 px-2.5 py-0.5 text-xs font-medium text-sage">
          no credentials
        </span>
      )}
      {readiness === "disabled" && (
        <span className="text-xs leading-relaxed text-ochre">
          Disabled on this deployment for safety.
        </span>
      )}
      {readiness === "missing" && (
        <span className="text-xs leading-relaxed text-inksoft">
          Driver not installed on the server.
        </span>
      )}
    </button>
  );
}

// ── Test-result panel ────────────────────────────────────────────────────────

function TestPanel({ result }: { result: ConnectorTestResponse }) {
  if (!result.ok) {
    return (
      <div className="rounded-2xl border-2 border-terra bg-terra/10 p-4 shadow-block-sm">
        <p className="font-display text-lg text-terra">Connection failed</p>
        <p className="mt-1 break-words text-sm leading-relaxed text-inksoft">
          {result.detail}
        </p>
      </div>
    );
  }
  return (
    <div className="rounded-2xl border-2 border-sage bg-sage/10 p-4 shadow-block-sm">
      <p className="font-display text-lg text-sage">Connected</p>
      <p className="mt-1 text-sm leading-relaxed text-inksoft">
        {result.detail}
      </p>
      {result.tables.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-1.5">
          {result.tables.map((t) => (
            <span
              key={t}
              className="rounded-full border border-ink/30 bg-paper2 px-2.5 py-0.5 font-mono text-xs text-ink"
            >
              {t}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Schema (introspection) cards ─────────────────────────────────────────────

function ColumnRow({ col }: { col: ColumnModel }) {
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-xl border border-line bg-paper px-3 py-2">
      <span className="font-mono text-sm text-ink">{col.name}</span>
      <Badge tone="frost">{col.data_type}</Badge>
      {col.nullable ? (
        <span className="text-xs font-medium text-frost">nullable</span>
      ) : (
        <span className="text-xs font-medium text-ochre">not null</span>
      )}
      {col.is_partition_key && (
        <span className="inline-flex items-center rounded-full bg-plum px-2.5 py-0.5 text-xs font-medium text-paper2">
          partition key
        </span>
      )}
    </div>
  );
}

function SchemaCard({ table }: { table: TableSchemaModel }) {
  const qualified = table.schema_name
    ? `${table.schema_name}.${table.name}`
    : table.name;
  return (
    <Card>
      <CardHeader>
        {table.schema_name && (
          <span className="font-mono text-[11px] uppercase tracking-widest text-inksoft">
            {table.schema_name}
          </span>
        )}
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="font-mono">{qualified}</CardTitle>
          {table.estimated_row_count != null && (
            <span className="font-mono text-xs text-inksoft">
              ~{fmtInt(table.estimated_row_count)} rows
            </span>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-1.5">
        {table.columns.map((col) => (
          <ColumnRow key={col.name} col={col} />
        ))}
      </CardContent>
    </Card>
  );
}

// ── Config form for the selected kind ────────────────────────────────────────

function ConfigForm({
  kind,
  form,
  onChange,
}: {
  kind: ConnectorKind;
  form: ConnectorFormState;
  onChange: (key: keyof ConnectorFormState, value: string) => void;
}) {
  if (kind === "duckdb") {
    return (
      <p className="rounded-2xl border-2 border-line bg-paper3/50 px-4 py-3 text-sm leading-relaxed text-inksoft">
        In-process demo database - seeded with realistic{" "}
        <span className="font-mono text-ink">raw.*</span> and{" "}
        <span className="font-mono text-ink">analytics.*</span> tables. No
        configuration required.
      </p>
    );
  }
  const fields = FIELDS_BY_KIND[kind];
  return (
    <div className="grid gap-4 sm:grid-cols-2">
      {fields.map((f) => {
        const id = `connect-${kind}-${f.key}`;
        return (
          <div
            key={f.key}
            className={cn(
              "flex flex-col gap-1",
              f.key === "dsn" && "sm:col-span-2",
            )}
          >
            <FieldLabel htmlFor={id}>{f.label}</FieldLabel>
            <input
              id={id}
              type={f.password ? "password" : "text"}
              value={form[f.key]}
              placeholder={f.placeholder}
              autoComplete="off"
              spellCheck={false}
              onChange={(e) => onChange(f.key, e.target.value)}
              className={INPUT_CLASS}
            />
          </div>
        );
      })}
    </div>
  );
}

// ── Main tab ─────────────────────────────────────────────────────────────────

export default function ConnectTab() {
  const { connection: activeConn, setConnection } = useAnalysis();
  const [connectors, setConnectors] = useState<ConnectorInfo[]>([]);
  const [loadingList, setLoadingList] = useState(true);
  const [listError, setListError] = useState<string | null>(null);

  const [selectedKind, setSelectedKind] = useState<ConnectorKind | null>(null);
  const [form, setForm] = useState<ConnectorFormState>(EMPTY_FORM);

  const [testResult, setTestResult] = useState<ConnectorTestResponse | null>(
    null,
  );
  const [tables, setTables] = useState<TableSchemaModel[] | null>(null);
  const [testing, setTesting] = useState(false);
  const [browsing, setBrowsing] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  // Load the connector catalog on mount; auto-select the first ready one.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const list = await getConnectors();
        if (cancelled) return;
        setConnectors(list);
        const firstReady = list.find((c) => c.available && c.enabled);
        if (firstReady) setSelectedKind(firstReady.kind);
      } catch (err) {
        if (!cancelled) setListError((err as Error).message);
      } finally {
        if (!cancelled) setLoadingList(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const updateField = useCallback(
    (key: keyof ConnectorFormState, value: string) => {
      setForm((prev) => ({ ...prev, [key]: value }));
    },
    [],
  );

  function selectKind(kind: ConnectorKind) {
    setSelectedKind(kind);
    // Results belong to a specific connector; clear when switching.
    setTestResult(null);
    setTables(null);
    setActionError(null);
  }

  async function handleTest(): Promise<ConnectorTestResponse | null> {
    if (!selectedKind) return null;
    setTesting(true);
    setActionError(null);
    try {
      const config = buildConfig(selectedKind, form);
      const result = await testConnector(config);
      setTestResult(result);
      return result;
    } catch (err) {
      setActionError((err as Error).message);
      return null;
    } finally {
      setTesting(false);
    }
  }

  async function handleBrowse() {
    if (!selectedKind) return;
    setBrowsing(true);
    setActionError(null);
    try {
      const config = buildConfig(selectedKind, form);
      const res = await introspectConnector(config);
      setTables(res.tables);
    } catch (err) {
      setActionError((err as Error).message);
    } finally {
      setBrowsing(false);
    }
  }

  /** DuckDB one-click: test then introspect in a single action. */
  async function handleConnectDemo() {
    setTesting(true);
    setBrowsing(true);
    setActionError(null);
    try {
      const config = buildConfig("duckdb", EMPTY_FORM);
      const result = await testConnector(config);
      setTestResult(result);
      if (result.ok) {
        const res = await introspectConnector(config);
        setTables(res.tables);
      }
    } catch (err) {
      setActionError((err as Error).message);
    } finally {
      setTesting(false);
      setBrowsing(false);
    }
  }

  const selectedInfo = connectors.find((c) => c.kind === selectedKind) ?? null;
  const isDuckDb = selectedKind === "duckdb";
  const busy = testing || browsing;

  return (
    <div className="space-y-8">
      {/* Header / intro */}
      <header className="space-y-3">
        <Eyebrow>Live database · Phase 2</Eyebrow>
        <h2 className="max-w-2xl font-display text-3xl leading-tight text-ink">
          Connect a real database.
        </h2>
        <p className="max-w-2xl leading-relaxed text-inksoft">
          Ground the analysis in real schemas and real cost. Browsing a live
          warehouse reads actual table shapes and{" "}
          <span className="text-ink">
            BigQuery dry-runs return the exact billed bytes
          </span>{" "}
          - no estimates. External connections may be disabled on the public
          demo for safety, while the in-process{" "}
          <span className="font-mono text-ink">DuckDB</span> demo always works.
        </p>
      </header>

      {/* Connector catalog */}
      <section className="space-y-3">
        <h3 className="font-mono text-[11px] font-semibold uppercase tracking-widest text-inksoft">
          Choose a connector
        </h3>

        {loadingList && (
          <div className="flex items-center gap-3 rounded-2xl border-2 border-line bg-paper3/50 px-4 py-3">
            <Loader2
              aria-hidden="true"
              className="h-5 w-5 animate-spin text-frost"
            />
            <span className="text-sm text-inksoft">Loading connectors…</span>
          </div>
        )}

        {!loadingList && listError && (
          <div className="rounded-2xl border-2 border-terra bg-terra/10 p-4 shadow-block-sm">
            <p className="font-display text-lg text-terra">
              Couldn&apos;t load connectors
            </p>
            <p className="mt-1 break-words text-sm leading-relaxed text-inksoft">
              {listError}
            </p>
          </div>
        )}

        {!loadingList && !listError && connectors.length === 0 && (
          <div className="flex flex-col items-center gap-4 rounded-2xl border-2 border-line bg-paper2 p-8 text-center shadow-block-sm">
            <ShapeBurst />
            <p className="max-w-sm text-sm leading-relaxed text-inksoft">
              No connectors are registered on this deployment.
            </p>
          </div>
        )}

        {!loadingList && !listError && connectors.length > 0 && (
          <div className="flex flex-wrap gap-3">
            {connectors.map((info) => (
              <ConnectorCard
                key={info.kind}
                info={info}
                selected={selectedKind === info.kind}
                onSelect={() => selectKind(info.kind)}
              />
            ))}
          </div>
        )}
      </section>

      {/* Config + actions for the selected connector */}
      {selectedInfo && (
        <section className="space-y-4">
          <h3 className="font-mono text-[11px] font-semibold uppercase tracking-widest text-inksoft">
            Configure · {selectedInfo.label}
          </h3>

          <ConfigForm
            kind={selectedInfo.kind}
            form={form}
            onChange={updateField}
          />

          {actionError && (
            <div className="rounded-2xl border-2 border-terra bg-terra/10 p-4 shadow-block-sm">
              <p className="font-display text-lg text-terra">Request failed</p>
              <p className="mt-1 break-words text-sm leading-relaxed text-inksoft">
                {actionError}
              </p>
            </div>
          )}

          <div className="flex flex-wrap items-center gap-3">
            {isDuckDb ? (
              <Button onClick={handleConnectDemo} loading={busy}>
                {!busy && <Sparkles aria-hidden="true" className="h-4 w-4" />}
                Connect demo DB
              </Button>
            ) : (
              <>
                <Button onClick={handleTest} loading={testing} disabled={busy}>
                  {!testing && <Plug aria-hidden="true" className="h-4 w-4" />}
                  Test connection
                </Button>
                <Button
                  variant="outline"
                  onClick={handleBrowse}
                  loading={browsing}
                  disabled={busy}
                >
                  {!browsing && (
                    <Search aria-hidden="true" className="h-4 w-4" />
                  )}
                  Browse schemas
                </Button>
              </>
            )}
          </div>

          {testResult && <TestPanel result={testResult} />}

          {testResult?.ok && selectedKind && (
            <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border-2 border-frost/40 bg-frost/10 p-4">
              {activeConn?.kind === selectedKind ? (
                <>
                  <span className="flex items-center gap-2 font-display text-lg text-frost">
                    <Plug aria-hidden="true" className="h-4 w-4" />
                    Active - Analyze &amp; the Agent are grounded in this
                    connection.
                  </span>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => setConnection(null)}
                  >
                    Stop using
                  </Button>
                </>
              ) : (
                <>
                  <span className="text-sm leading-relaxed text-inksoft">
                    Use this connection so{" "}
                    <span className="font-medium text-ink">Analyze</span> and the{" "}
                    <span className="font-medium text-ink">Agent</span> resolve
                    real schemas and real (profiled) cost.
                  </span>
                  <Button
                    size="sm"
                    onClick={() =>
                      setConnection(buildConfig(selectedKind, form))
                    }
                  >
                    Use for analysis
                  </Button>
                </>
              )}
            </div>
          )}
        </section>
      )}

      {/* Introspected schemas */}
      {tables && (
        <section className="space-y-4">
          <h3 className="flex items-center gap-2 font-mono text-[11px] font-semibold uppercase tracking-widest text-inksoft">
            <Table2 aria-hidden="true" className="h-3.5 w-3.5 text-frost" />
            Schemas · {tables.length} table{tables.length === 1 ? "" : "s"}
          </h3>
          {tables.length === 0 ? (
            <p className="rounded-2xl border-2 border-line bg-paper3/50 px-4 py-3 text-sm leading-relaxed text-inksoft">
              No tables were found for this connection.
            </p>
          ) : (
            <div className="grid gap-4 lg:grid-cols-2">
              {tables.map((table) => (
                <SchemaCard
                  key={`${table.schema_name ?? ""}.${table.name}`}
                  table={table}
                />
              ))}
            </div>
          )}
        </section>
      )}
    </div>
  );
}
