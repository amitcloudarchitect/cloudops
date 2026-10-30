"use client";

// Customer mapping (step 14b, HLD M1, ADR 0015): the names sources send that no
// tenant matched, and the governed path from a name to a tenant. A person
// proposes; a second person approves where the platform requires two (UAT and
// production); the database enforces it either way. A mapping applies to what is
// ingested from then on; records held before it move only when someone asks for a
// re-process, which the connector worker runs (the console starts nothing itself).

import { useEffect, useState } from "react";
import type { FormEvent } from "react";

import type { ProposalView, QuarantinedAlias, ReprocessPreview, ReprocessView, TenantAliases } from "@cloudops/contracts";

import { DataTable, type Column } from "@/components/DataTable";
import { EmptyState } from "@/components/EmptyState";
import { HealthBadge } from "@/components/HealthBadge";
import { LoadError, Loading } from "@/components/Loading";
import { MetricCard } from "@/components/MetricCard";
import { PageHeader } from "@/components/PageHeader";
import { Provenance } from "@/components/Provenance";
import { ApiError, apiGet, apiPost } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { count, date, dateTime } from "@/lib/format";
import { useMe } from "@/lib/session";
import { useApi } from "@/lib/useApi";

const control = "w-full rounded-control border border-line bg-surface px-2 py-1.5 text-sm text-ink";
const label = "flex flex-col gap-1 text-xs font-medium text-muted";
const primary = "rounded-control bg-accent px-3 py-1.5 text-sm font-medium text-accent-ink disabled:opacity-60";
const secondary = "rounded-control border border-line px-3 py-1.5 text-sm font-medium text-ink disabled:opacity-60";

type Message = { kind: "ok" | "error"; text: string } | null;

function failure(error: unknown, what: string): string {
  if (error instanceof ApiError) {
    if (error.code === "second_approver_required") return `${what} was not saved: a second person must approve a mapping you proposed.`;
    if (error.status === 409) return `${what} was not saved: that name or customer already exists, is already mapped, or was already decided.`;
    if (error.status === 404) return `${what} was not saved: the customer or proposal is not in your scope.`;
    if (error.status === 422) return `${what} was not saved: check the values.`;
  }
  return `${what} was not saved. Try again in a moment.`;
}

function failureReprocess(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === "nothing_held") return "Nothing was held under this name, so there is nothing to re-process.";
    if (error.code === "mapping_changed") return "This name no longer maps to this customer, so its records are not re-processed.";
    if (error.code === "not_approved") return "Only an approved mapping is re-processed.";
    if (error.status === 409) return "A re-process of this name is already waiting or running.";
  }
  return failure(error, "The request");
}

function Feedback({ message }: { message: Message }) {
  if (!message) return null;
  return (
    <p role={message.kind === "error" ? "alert" : "status"} className={`text-sm ${message.kind === "error" ? "text-band-critical" : "text-band-healthy"}`}>
      {message.text}
    </p>
  );
}

// --- mapping a name --------------------------------------------------------------

interface Target {
  source: string;
  alias: string;
  suggested: string | null;
  fromQuarantine: boolean;
}

function MapForm({ target, tenants, onDone, onCancel }: { target: Target; tenants: TenantAliases[]; onDone: (text: string) => void; onCancel: () => void }) {
  const { token } = useAuth();
  const [source, setSource] = useState(target.source);
  const [alias, setAlias] = useState(target.alias);
  const [mode, setMode] = useState<"existing" | "new">(tenants.length === 0 ? "new" : "existing");
  const [tenantId, setTenantId] = useState(target.suggested ?? "");
  const [name, setName] = useState("");
  const [code, setCode] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<Message>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setMessage(null);
    if (!alias.trim() || !/^[a-z0-9_]{1,40}$/.test(source)) {
      setMessage({ kind: "error", text: "Give the name exactly as the source sends it, and its source system." });
      return;
    }
    if (mode === "existing" && !tenantId) {
      setMessage({ kind: "error", text: "Choose the customer this name belongs to." });
      return;
    }
    if (mode === "new" && (!name.trim() || !/^[A-Z0-9]{2,10}$/.test(code))) {
      setMessage({ kind: "error", text: "A new customer needs a name and a short code of 2 to 10 capitals or digits." });
      return;
    }
    setBusy(true);
    let onboarded: string | null = null;
    try {
      let tenant = tenantId;
      if (mode === "new") {
        const created = await apiPost(await token(), "/v1/mapping/tenants", {}, { canonical_name: name.trim(), short_code: code });
        tenant = created.tenant.id;
        onboarded = created.tenant.name;
      }
      await apiPost(await token(), "/v1/mapping/proposals", {}, { source_system: source, alias, tenant_id: tenant, note: note.trim() || null });
      onDone(onboarded ? `${onboarded} onboarded, and the mapping proposed.` : "Mapping proposed.");
    } catch (error) {
      const prefix = onboarded ? `${onboarded} was onboarded, but the mapping` : "The mapping";
      setMessage({ kind: "error", text: failure(error, prefix) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} aria-labelledby="map-heading" className="space-y-3 rounded-card border border-line bg-surface p-4">
      <h3 id="map-heading" className="text-base font-semibold text-ink">
        Map a name to a customer
      </h3>
      <div className="grid gap-3 sm:grid-cols-3">
        <label className={label}>
          Source system
          <input className={control} value={source} onChange={(e) => setSource(e.target.value)} readOnly={target.fromQuarantine} maxLength={40} required />
        </label>
        <label className={`${label} sm:col-span-2`}>
          Name, exactly as the source sends it
          <input className={control} value={alias} onChange={(e) => setAlias(e.target.value)} readOnly={target.fromQuarantine} maxLength={200} required />
        </label>
      </div>
      <fieldset className="space-y-2">
        <legend className="text-xs font-medium text-muted">Customer</legend>
        <div className="flex flex-wrap gap-4 text-sm text-ink">
          <label className="flex items-center gap-2">
            <input type="radio" name="mode" checked={mode === "existing"} onChange={() => setMode("existing")} disabled={tenants.length === 0} />
            An existing customer
          </label>
          <label className="flex items-center gap-2">
            <input type="radio" name="mode" checked={mode === "new"} onChange={() => setMode("new")} />
            Onboard a new customer
          </label>
        </div>
        {mode === "existing" ? (
          <label className={label}>
            Customer to map to
            <select className={control} value={tenantId} onChange={(e) => setTenantId(e.target.value)}>
              <option value="">Choose…</option>
              {tenants.map((t) => (
                <option key={t.tenant.id} value={t.tenant.id}>
                  {t.tenant.name} ({t.short_code}){t.tenant.id === target.suggested ? " — suggested" : ""}
                </option>
              ))}
            </select>
          </label>
        ) : (
          <div className="grid gap-3 sm:grid-cols-3">
            <label className={`${label} sm:col-span-2`}>
              Canonical name
              <input className={control} value={name} onChange={(e) => setName(e.target.value)} maxLength={200} />
            </label>
            <label className={label}>
              Short code
              <input className={control} value={code} onChange={(e) => setCode(e.target.value.toUpperCase())} maxLength={10} placeholder="e.g. NWL" />
            </label>
          </div>
        )}
      </fieldset>
      <label className={label}>
        Note for the approver (optional)
        <textarea className={control} rows={2} maxLength={1000} value={note} onChange={(e) => setNote(e.target.value)} />
      </label>
      <Feedback message={message} />
      <div className="flex gap-2">
        <button type="submit" disabled={busy} className={primary}>
          {mode === "new" ? "Onboard and propose" : "Propose mapping"}
        </button>
        <button type="button" onClick={onCancel} className={secondary}>
          Cancel
        </button>
      </div>
    </form>
  );
}

// --- deciding ----------------------------------------------------------------------

function PendingRow({ proposal, approvers, onDecided }: { proposal: ProposalView; approvers: number; onDecided: (text: string) => void }) {
  const { token } = useAuth();
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<Message>(null);

  async function decide(decision: "approve" | "reject") {
    if (decision === "reject" && !reason.trim()) {
      setMessage({ kind: "error", text: "Say why it is rejected." });
      return;
    }
    setBusy(true);
    setMessage(null);
    try {
      await apiPost(await token(), "/v1/mapping/proposals/{proposal_id}/decision", { proposal_id: proposal.id }, { decision, reason: decision === "reject" ? reason.trim() : null });
      onDecided(decision === "approve" ? `“${proposal.alias}” now maps to ${proposal.tenant.name} for new ${proposal.source_system} records.` : "Proposal rejected.");
    } catch (error) {
      setMessage({ kind: "error", text: failure(error, "The decision") });
    } finally {
      setBusy(false);
    }
  }

  return (
    <li className="rounded-card border border-line bg-surface p-3" data-testid="pending-proposal">
      <p className="text-sm text-ink">
        <span className="font-semibold">“{proposal.alias}”</span> <span className="text-muted">from {proposal.source_system} →</span>{" "}
        <span className="font-semibold">{proposal.tenant.name}</span>
      </p>
      <p className="text-xs text-muted">
        Proposed {proposal.proposed_by_me ? "by you" : "by another person"} · {dateTime(proposal.proposed_at)}
      </p>
      {proposal.note ? <p className="mt-1 text-sm text-muted">“{proposal.note}”</p> : null}
      <div className="mt-2 space-y-2">
        <Feedback message={message} />
        {rejecting ? (
          <div className="flex flex-wrap items-end gap-2">
            <label className={`${label} min-w-0 flex-1`}>
              Reason for rejecting
              <input className={control} value={reason} onChange={(e) => setReason(e.target.value)} maxLength={1000} />
            </label>
            <button type="button" disabled={busy} onClick={() => void decide("reject")} className={secondary}>
              Reject
            </button>
            <button type="button" onClick={() => setRejecting(false)} className={secondary}>
              Cancel
            </button>
          </div>
        ) : (
          <div className="flex flex-wrap items-center gap-2">
            {proposal.may_approve ? (
              <button type="button" disabled={busy} onClick={() => void decide("approve")} className={primary}>
                Approve
              </button>
            ) : (
              <HealthBadge band="watch" label={approvers > 1 ? "Needs a second person to approve" : "Awaiting approval"} />
            )}
            <button type="button" disabled={busy} onClick={() => setRejecting(true)} className={secondary}>
              {proposal.proposed_by_me ? "Withdraw…" : "Reject…"}
            </button>
          </div>
        )}
      </div>
    </li>
  );
}

// --- re-processing what an approved name held --------------------------------------

const REPROCESS_STATE: Record<ReprocessView["status"], { label: string; band: "healthy" | "watch" | "at-risk" | "critical" | "unknown" }> = {
  requested: { label: "Waiting for the worker", band: "watch" },
  running: { label: "Running", band: "watch" },
  succeeded: { label: "Done", band: "healthy" },
  refused: { label: "Refused", band: "at-risk" },
  failed: { label: "Failed", band: "critical" },
};

const OUTCOME: Record<string, string> = {
  mapping_changed: "the name no longer mapped to this customer when the worker checked",
  not_approved: "the mapping was no longer approved when the worker checked",
  page_dead_lettered: "a bronze page kept failing and was dead-lettered for the platform team",
};

function ReprocessStatus({ reprocess }: { reprocess: ReprocessView }) {
  const state = REPROCESS_STATE[reprocess.status];
  return (
    <span className="mt-1 block text-xs text-muted" data-testid="reprocess-status">
      <HealthBadge band={state.band} label={state.label} /> Re-process requested {reprocess.requested_by_me ? "by you" : "by another person"},{" "}
      {dateTime(reprocess.requested_at)}.
      {reprocess.moved ? (
        <span className="block text-ink">
          {count(reprocess.moved.value)} held records moved to this customer. <Provenance metric={reprocess.moved} />
        </span>
      ) : null}
      {reprocess.outcome ? <span className="block">Why: {OUTCOME[reprocess.outcome] ?? reprocess.outcome}.</span> : null}
      {reprocess.status === "requested" || reprocess.status === "running" ? (
        <span className="block">The connector worker picks requests up within a minute; this page refreshes until it ends.</span>
      ) : null}
    </span>
  );
}

function ReprocessAction({ proposal, onRequested }: { proposal: ProposalView; onRequested: (text: string) => void }) {
  const { token } = useAuth();
  const [preview, setPreview] = useState<ReprocessPreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<Message>(null);

  async function open() {
    setBusy(true);
    setMessage(null);
    try {
      setPreview(await apiGet(await token(), "/v1/mapping/proposals/{proposal_id}/reprocess", { proposal_id: proposal.id }));
    } catch (error) {
      setMessage({ kind: "error", text: failureReprocess(error) });
    } finally {
      setBusy(false);
    }
  }

  async function request() {
    setBusy(true);
    setMessage(null);
    try {
      await apiPost(await token(), "/v1/mapping/proposals/{proposal_id}/reprocess", { proposal_id: proposal.id }, undefined);
      onRequested(`Re-process of “${proposal.alias}” requested.`);
    } catch (error) {
      setMessage({ kind: "error", text: failureReprocess(error) });
    } finally {
      setBusy(false);
    }
  }

  if (!preview) {
    return (
      <div className="mt-2 space-y-2">
        <Feedback message={message} />
        <button type="button" disabled={busy} onClick={() => void open()} className={secondary}>
          Re-process held records…
        </button>
      </div>
    );
  }
  return (
    <div className="mt-2 space-y-2 rounded-control bg-surface-raised p-3" role="group" aria-label={`Re-process ${proposal.alias}`}>
      {preview.upper_bound ? (
        <p className="text-sm text-ink">
          Up to <strong>{count(preview.upper_bound.value)}</strong> held sightings, from {date(preview.window_start)} to {date(preview.window_end)}.{" "}
          <Provenance metric={preview.upper_bound} />
        </p>
      ) : null}
      <p className="text-xs text-muted">
        An upper bound: a record read twice was counted twice. The worker replays those days from the raw store for this name only, never
        re-fetching from the source, and reports exactly how many records moved.
      </p>
      <Feedback message={message} />
      {preview.available ? (
        <div className="flex gap-2">
          <button type="button" disabled={busy} onClick={() => void request()} className={primary}>
            Request re-process
          </button>
          <button type="button" onClick={() => setPreview(null)} className={secondary}>
            Cancel
          </button>
        </div>
      ) : (
        <p role="status" className="text-sm text-muted">
          Not available: {preview.reason === "already_requested" ? "a re-process is already waiting or running" : failureReprocess(new ApiError(409, preview.reason ?? "conflict", null))}
        </p>
      )}
    </div>
  );
}

function DecidedRow({ proposal, onRequested }: { proposal: ProposalView; onRequested: (text: string) => void }) {
  const decision = proposal.decision;
  const approved = decision?.decision === "approve";
  const last = proposal.reprocess;
  const mayAsk = approved && !!decision?.held_occurrences && (!last || last.status === "refused" || last.status === "failed");
  return (
    <li className="rounded-card border border-line bg-surface p-3 text-sm text-ink" data-testid="decided-proposal">
      <span className="font-semibold">“{proposal.alias}”</span> <span className="text-muted">from {proposal.source_system} →</span> {proposal.tenant.name}{" "}
      <HealthBadge band={approved ? "healthy" : "unknown"} label={approved ? "Approved" : "Rejected"} />
      <span className="block text-xs text-muted">
        {dateTime(decision?.decided_at)}
        {decision?.held_occurrences ? ` · ${count(decision.held_occurrences.value)} sightings were held before approval` : ""}
      </span>
      {decision?.reason ? <span className="block text-muted">“{decision.reason}”</span> : null}
      {last ? <ReprocessStatus reprocess={last} /> : null}
      {mayAsk ? <ReprocessAction key={last?.id ?? "none"} proposal={proposal} onRequested={onRequested} /> : null}
    </li>
  );
}

// --- the screen --------------------------------------------------------------------

interface Data {
  quarantine: Awaited<ReturnType<typeof apiGet<"/v1/mapping/quarantine">>>;
  tenants: TenantAliases[];
  pending: ProposalView[];
  decided: ProposalView[];
}

async function load(token: string): Promise<Data> {
  const [quarantine, tenants, pending, decided] = await Promise.all([
    apiGet(token, "/v1/mapping/quarantine"),
    apiGet(token, "/v1/mapping/tenants"),
    apiGet(token, "/v1/mapping/proposals", {}, { status: "pending" }),
    apiGet(token, "/v1/mapping/proposals", {}, { status: "decided" }),
  ]);
  return { quarantine, tenants, pending, decided };
}

export function CustomerMapping() {
  const me = useMe();
  const loaded = useApi(load, "mapping");
  const [target, setTarget] = useState<Target | null>(null);
  const [message, setMessage] = useState<Message>(null);
  const waiting = loaded.status === "ready" && loaded.data.decided.some((p) => p.reprocess?.status === "requested" || p.reprocess?.status === "running");
  const { reload } = loaded;
  useEffect(() => {
    if (!waiting) return;
    const timer = window.setInterval(reload, 10_000);
    return () => window.clearInterval(timer);
  }, [waiting, reload]);

  if (loaded.status === "loading") return <Loading what="the customer mapping" />;
  if (loaded.status === "error") return <LoadError error={loaded.error} what="customer mapping" />;
  const { quarantine, tenants, pending, decided } = loaded.data;
  const approvers = quarantine.approvers_required;

  function done(text: string) {
    setTarget(null);
    setMessage({ kind: "ok", text });
    loaded.reload();
  }

  const quarantineColumns: Column<QuarantinedAlias>[] = [
    { key: "alias", header: "Name as sent", value: (q) => q.alias, sortable: true, className: "break-words" },
    { key: "source", header: "Source", value: (q) => q.source_system, sortable: true },
    { key: "sightings", header: "Sightings", value: (q) => q.occurrences.value, render: (q) => count(q.occurrences.value), sortable: true, className: "tabular-nums" },
    { key: "last", header: "Last seen", value: (q) => q.last_seen, render: (q) => dateTime(q.last_seen), sortable: true },
    { key: "suggested", header: "Suggested customer", value: (q) => q.suggested_tenant?.name ?? null, render: (q) => q.suggested_tenant?.name ?? <span className="text-muted">None</span> },
    {
      key: "action",
      header: "Action",
      value: () => null,
      render: (q) =>
        q.pending_proposal_id ? (
          <HealthBadge band="watch" label="Proposed" />
        ) : (
          <button
            type="button"
            className="font-medium text-accent underline"
            aria-label={`Map ${q.alias}`}
            onClick={() => setTarget({ source: q.source_system, alias: q.alias, suggested: q.suggested_tenant?.id ?? null, fromQuarantine: true })}
          >
            Map…
          </button>
        ),
    },
  ];

  const tenantColumns: Column<TenantAliases>[] = [
    { key: "name", header: "Customer", value: (t) => t.tenant.name, sortable: true },
    { key: "code", header: "Short code", value: (t) => t.short_code, sortable: true },
    { key: "status", header: "Status", value: (t) => t.status, sortable: true },
    {
      key: "aliases",
      header: "Names per source",
      value: (t) => t.aliases.map((a) => `${a.source_system}: ${a.alias}`).join("; "),
      render: (t) =>
        t.aliases.length === 0 ? (
          <span className="text-muted">None yet</span>
        ) : (
          <ul className="space-y-0.5">
            {t.aliases.map((a) => (
              <li key={`${a.source_system}:${a.alias}`} className="break-words">
                <span className="text-muted">{a.source_system}:</span> {a.alias}
              </li>
            ))}
          </ul>
        ),
    },
  ];

  return (
    <>
      <PageHeader
        title="Customer mapping"
        subtitle={
          approvers > 1
            ? "Names the sources send, mapped to customers. A mapping needs a second person to approve it."
            : "Names the sources send, mapped to customers. In this environment one person may propose and approve."
        }
      >
        <button type="button" className={secondary} onClick={() => setTarget({ source: "servicenow", alias: "", suggested: null, fromQuarantine: false })}>
          Map a name or onboard a customer…
        </button>
      </PageHeader>

      <div className="mb-6 grid gap-3 sm:grid-cols-2">
        <MetricCard label="Names in quarantine" metric={quarantine.names} display={(m) => count(Number(m.value))} />
        <MetricCard label="Sightings held" metric={quarantine.sightings} display={(m) => count(Number(m.value))} />
      </div>

      <div className="space-y-6">
        <Feedback message={message} />
        {target ? <MapForm key={`${target.source}:${target.alias}`} target={target} tenants={tenants} onDone={done} onCancel={() => setTarget(null)} /> : null}

        <section aria-labelledby="pending-heading">
          <h2 id="pending-heading" className="mb-2 text-lg font-semibold text-ink">
            Awaiting approval ({pending.length})
          </h2>
          {pending.length === 0 ? (
            <EmptyState title="Nothing awaiting approval" next="Proposed mappings appear here until someone approves or rejects them." />
          ) : (
            <ul className="space-y-2">
              {pending.map((p) => (
                <PendingRow key={p.id} proposal={p} approvers={approvers} onDecided={done} />
              ))}
            </ul>
          )}
        </section>

        <section aria-labelledby="quarantine-heading">
          <h2 id="quarantine-heading" className="mb-2 text-lg font-semibold text-ink">
            Quarantined names
          </h2>
          <p className="mb-2 text-sm text-muted">
            Records whose company matched no customer are held, never guessed. Most sighted first. A sighting counts each time a record is read, so
            overlapping fetches and replays count again.
          </p>
          <DataTable
            id="mapping-quarantine"
            caption="Quarantined names"
            rows={quarantine.items}
            rowKey={(q) => q.id}
            columns={quarantineColumns}
            facets={[{ key: "source", label: "Source", value: (q) => q.source_system }]}
            role={me.role}
            initial={{ sort: { key: "sightings", dir: "desc" } }}
            empty={{ title: "Nothing in quarantine", next: "Every name the sources sent matched a customer." }}
          />
        </section>

        <section aria-labelledby="tenants-heading">
          <h2 id="tenants-heading" className="mb-2 text-lg font-semibold text-ink">
            Customers and their names
          </h2>
          <DataTable
            id="mapping-tenants"
            caption="Customers and their names"
            rows={tenants}
            rowKey={(t) => t.tenant.id}
            columns={tenantColumns}
            role={me.role}
            empty={{ title: "No customers in your scope", next: "Onboard a customer, or ask a platform administrator to assign customers to you." }}
          />
        </section>

        <section aria-labelledby="decided-heading">
          <h2 id="decided-heading" className="mb-2 text-lg font-semibold text-ink">
            Decided
          </h2>
          {decided.length === 0 ? (
            <EmptyState title="No decisions yet" next="Approved and rejected mappings are listed here, newest first." />
          ) : (
            <ul className="space-y-2">
              {decided.map((p) => (
                <DecidedRow key={p.id} proposal={p} onRequested={done} />
              ))}
            </ul>
          )}
        </section>
      </div>
    </>
  );
}
