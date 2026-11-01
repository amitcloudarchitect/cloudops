"use client";

// Settings → Manage customers (ADR 0038). A customer master renames a customer or
// changes its short code. A platform administrator deletes one: it is offboarded at
// once (gone from every list, selector and dashboard, its cloud credentials revoked,
// its portal suspended, its names quarantined), may be restored within the retention,
// and may be purged after it. The purge is a request the purge worker runs; the
// console starts nothing. The database checks every one of these again.

import { useState } from "react";
import type { FormEvent } from "react";

import type { OffboardedTenant, TenantAliases } from "@cloudops/contracts";

import { DataTable, type Column } from "@/components/DataTable";
import { EmptyState } from "@/components/EmptyState";
import { LoadError, Loading } from "@/components/Loading";
import { PageHeader } from "@/components/PageHeader";
import { ApiError, apiGet, apiPost } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { date, dateTime } from "@/lib/format";
import { can, useMe, useReloadSession } from "@/lib/session";
import { useApi } from "@/lib/useApi";

const control = "w-full field";
const label = "flex flex-col gap-1 text-xs font-semibold text-muted";
const primary = "btn btn-primary";
const secondary = "btn btn-secondary";

type Message = { kind: "ok" | "error"; text: string } | null;

const REFUSALS: Record<string, string> = {
  confirmation_mismatch: "the name or short code typed does not match exactly.",
  short_code_in_use: "a stored cloud credential is named by this short code. Revoke it first, or keep the code.",
  platform_host_rule: "the tenant that hosts the platform cannot be deleted. Move the platform host first.",
  retention_not_ended: "the retention period has not ended yet.",
  retention_ended: "the retention period has ended; the customer can only be purged now.",
  tenant_state: "the customer is not in a state that allows this (it may have changed since you loaded the page).",
  conflict: "another customer already has that name or short code.",
  not_found: "the customer is not in your scope.",
  forbidden: "your role does not allow it.",
  invalid_request: "check the values.",
};

function failure(error: unknown, what: string): string {
  if (error instanceof ApiError && REFUSALS[error.code]) return `${what} was refused: ${REFUSALS[error.code]}`;
  return `${what} did not complete. Try again in a moment.`;
}

function Feedback({ message }: { message: Message }) {
  if (!message) return null;
  return (
    <p role={message.kind === "error" ? "alert" : "status"} className={`text-sm ${message.kind === "error" ? "text-band-critical" : "text-band-healthy"}`}>
      {message.text}
    </p>
  );
}

// --- edit ------------------------------------------------------------------------

function EditForm({ tenant, onDone, onCancel }: { tenant: TenantAliases; onDone: (text: string) => void; onCancel: () => void }) {
  const { token } = useAuth();
  const [name, setName] = useState(tenant.tenant.name);
  const [code, setCode] = useState(tenant.short_code);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<Message>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setMessage(null);
    try {
      const saved = await apiPost(await token(), "/v1/tenants/{tenant_id}/edit", { tenant_id: tenant.tenant.id }, { canonical_name: name, short_code: code });
      onDone(`Saved: ${saved.tenant.name} (${saved.short_code}). Its mappings are unchanged.`);
    } catch (error) {
      setMessage({ kind: "error", text: failure(error, "The change") });
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} aria-labelledby="edit-heading" className="space-y-3 card p-4">
      <h2 id="edit-heading" className="section-title">
        Edit {tenant.tenant.name}
      </h2>
      <div className="grid gap-3 sm:grid-cols-2">
        <label className={label}>
          Customer name
          <input className={control} value={name} onChange={(e) => setName(e.target.value)} maxLength={200} required />
        </label>
        <label className={label}>
          Short code (2 to 10 capitals or digits)
          <input
            className={control}
            value={code}
            onChange={(e) => setCode(e.target.value.toUpperCase())}
            pattern="[A-Z0-9]{2,10}"
            maxLength={10}
            required
            aria-describedby="code-help"
          />
        </label>
      </div>
      <p id="code-help" className="text-xs text-muted">
        The short code names a stored cloud credential&apos;s secrets and schedules, so it cannot change while the customer has one that is not revoked. Mappings of
        source names are kept by customer, not by name, and do not change.
      </p>
      <Feedback message={message} />
      <div className="flex flex-wrap gap-2">
        <button type="submit" className={primary} disabled={busy}>
          Save changes
        </button>
        <button type="button" className={secondary} onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}

// --- delete (offboard) -------------------------------------------------------------

function OffboardForm({ tenant, onDone, onCancel }: { tenant: TenantAliases; onDone: (text: string) => void; onCancel: () => void }) {
  const { token } = useAuth();
  const [typed, setTyped] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<Message>(null);
  const name = tenant.tenant.name;
  const ready = typed === name && reason.trim().length > 0;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!ready) return;
    setBusy(true);
    setMessage(null);
    try {
      const done = await apiPost(await token(), "/v1/tenants/{tenant_id}/offboard", { tenant_id: tenant.tenant.id }, { confirm_name: typed, reason });
      onDone(`${name} is offboarded. It can be restored until ${date(done.purge_after)}, and purged after that.`);
    } catch (error) {
      setMessage({ kind: "error", text: failure(error, "Deleting the customer") });
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} aria-labelledby="offboard-heading" className="space-y-3 card border-band-critical p-4">
      <h2 id="offboard-heading" className="section-title">
        Delete {name}
      </h2>
      <div id="offboard-effects" className="space-y-1 text-sm text-ink">
        <p>Deleting offboards the customer now. Straight away:</p>
        <ul className="list-disc pl-5">
          <li>it disappears from every list, the customer selector and every dashboard, for everyone;</li>
          <li>its connectors stop: its stored cloud credentials are revoked, and records for its names are held in quarantine;</li>
          <li>its portal users lose access.</li>
        </ul>
        <p>
          Its data is kept for the retention period, during which a platform administrator can restore it. After that it can be purged. The audit log is never
          deleted.
        </p>
      </div>
      <label className={label}>
        Type the customer&apos;s name exactly to confirm: {name}
        <input className={control} value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" aria-describedby="offboard-effects" />
      </label>
      <label className={label}>
        Reason (kept in the audit trail)
        <textarea className={control} value={reason} onChange={(e) => setReason(e.target.value)} maxLength={1000} rows={2} />
      </label>
      <Feedback message={message} />
      <div className="flex flex-wrap gap-2">
        <button type="submit" className={primary} disabled={busy || !ready}>
          Delete customer
        </button>
        <button type="button" className={secondary} onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}

// --- purge: two confirmations ----------------------------------------------------------

function PurgeForm({ tenant, onDone, onCancel }: { tenant: OffboardedTenant; onDone: (text: string) => void; onCancel: () => void }) {
  const { token } = useAuth();
  const [typedName, setTypedName] = useState("");
  const [typedCode, setTypedCode] = useState("");
  const [step, setStep] = useState<1 | 2>(1);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<Message>(null);
  const name = tenant.tenant.name;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (step === 1) {
      if (typedName === name) setStep(2);
      return;
    }
    if (typedCode !== tenant.short_code) return;
    setBusy(true);
    setMessage(null);
    try {
      await apiPost(await token(), "/v1/tenants/{tenant_id}/purge", { tenant_id: tenant.tenant.id }, { confirm_name: typedName, confirm_code: typedCode });
      onDone(`The purge of ${name} is requested. The purge worker runs it within a few minutes; its status shows below.`);
    } catch (error) {
      setMessage({ kind: "error", text: failure(error, "The purge") });
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} aria-labelledby="purge-heading" className="space-y-3 card border-band-critical p-4">
      <h2 id="purge-heading" className="section-title">
        Purge {name} permanently
      </h2>
      <p id="purge-effects" className="text-sm text-ink">
        A purge deletes every record of this customer from the platform, and its stored cloud data. It cannot be undone. The audit log and a record that the
        customer existed are kept.
      </p>
      {step === 1 ? (
        <label className={label}>
          Step 1 of 2. Type the customer&apos;s name exactly: {name}
          <input className={control} value={typedName} onChange={(e) => setTypedName(e.target.value)} autoComplete="off" aria-describedby="purge-effects" />
        </label>
      ) : (
        <label className={label}>
          Step 2 of 2. Type its short code to purge it: {tenant.short_code}
          <input className={control} value={typedCode} onChange={(e) => setTypedCode(e.target.value)} autoComplete="off" aria-describedby="purge-effects" />
        </label>
      )}
      <Feedback message={message} />
      <div className="flex flex-wrap gap-2">
        {step === 1 ? (
          <button type="submit" className={primary} disabled={typedName !== name}>
            Continue
          </button>
        ) : (
          <button type="submit" className={primary} disabled={busy || typedCode !== tenant.short_code}>
            Purge permanently
          </button>
        )}
        <button type="button" className={secondary} onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}

const PURGE_STATUS: Record<string, string> = {
  requested: "Purge requested",
  running: "Purging",
  succeeded: "Purged",
  failed: "Purge failed",
};

function lifecycle(t: OffboardedTenant): string {
  if (t.status === "purged") return `Purged ${dateTime(t.purged_at)}`;
  if (t.purge) return PURGE_STATUS[t.purge.status] ?? t.purge.status;
  return t.may_restore ? `Restorable until ${date(t.purge_after)}` : "Retention ended";
}

// --- the screen --------------------------------------------------------------------------

type Open = { kind: "edit" | "offboard"; tenant: TenantAliases } | { kind: "purge"; tenant: OffboardedTenant } | null;

export function ManageCustomers() {
  const me = useMe();
  const reloadSession = useReloadSession();
  const { token } = useAuth();
  const admin = me.role_class === "platform_admin" && can(me, "access.manage");
  const live = useApi((t) => apiGet(t, "/v1/mapping/tenants"), "manage-customers");
  const gone = useApi(async (t) => (admin ? apiGet(t, "/v1/tenants/offboarded") : []), `offboarded:${admin}`);
  const [open, setOpen] = useState<Open>(null);
  const [message, setMessage] = useState<Message>(null);
  const [busy, setBusy] = useState(false);

  function done(text: string) {
    setOpen(null);
    setMessage({ kind: "ok", text });
    live.reload();
    gone.reload();
    // The selector and every list read the user's customers from /v1/me.
    reloadSession();
  }

  async function restore(tenant: OffboardedTenant) {
    setBusy(true);
    setMessage(null);
    try {
      await apiPost(await token(), "/v1/tenants/{tenant_id}/restore", { tenant_id: tenant.tenant.id }, undefined);
      done(
        `${tenant.tenant.name} is restored. Its cloud credentials stay revoked and its portal suspended: onboard them again in Cloud onboarding and Portal onboarding.`,
      );
    } catch (error) {
      setMessage({ kind: "error", text: failure(error, "Restoring the customer") });
    } finally {
      setBusy(false);
    }
  }

  if (live.status === "loading") return <Loading what="customers" />;
  if (live.status === "error") return <LoadError error={live.error} what="customers" />;

  const columns: Column<TenantAliases>[] = [
    { key: "name", header: "Customer", value: (t) => t.tenant.name, sortable: true },
    { key: "code", header: "Short code", value: (t) => t.short_code, sortable: true },
    { key: "kind", header: "Kind", value: (t) => (t.hosts_platform ? "Internal, hosts the platform" : t.internal ? "Internal" : "Customer") },
    {
      key: "actions",
      header: "Actions",
      value: () => null,
      render: (t) => (
        <div className="flex flex-wrap gap-2">
          <button type="button" className={secondary} aria-label={`Edit ${t.tenant.name}`} onClick={() => setOpen({ kind: "edit", tenant: t })}>
            Edit
          </button>
          {admin ? (
            <button
              type="button"
              className={secondary}
              aria-label={`Delete ${t.tenant.name}`}
              disabled={t.hosts_platform}
              title={t.hosts_platform ? "The platform host cannot be deleted" : undefined}
              onClick={() => setOpen({ kind: "offboard", tenant: t })}
            >
              Delete
            </button>
          ) : null}
        </div>
      ),
    },
  ];

  const goneColumns: Column<OffboardedTenant>[] = [
    { key: "name", header: "Customer", value: (t) => t.tenant.name, sortable: true },
    { key: "code", header: "Short code", value: (t) => t.short_code },
    { key: "offboarded", header: "Deleted", value: (t) => t.offboarded_at, render: (t) => `${dateTime(t.offboarded_at)}${t.offboarded_by_me ? " by you" : ""}`, sortable: true },
    { key: "reason", header: "Reason", value: (t) => t.reason },
    { key: "state", header: "State", value: (t) => lifecycle(t) },
    {
      key: "actions",
      header: "Actions",
      value: () => null,
      render: (t) => (
        <div className="flex flex-wrap gap-2">
          {t.may_restore ? (
            <button type="button" className={secondary} aria-label={`Restore ${t.tenant.name}`} disabled={busy} onClick={() => restore(t)}>
              Restore
            </button>
          ) : null}
          {t.may_purge ? (
            <button type="button" className={secondary} aria-label={`Purge ${t.tenant.name}`} onClick={() => setOpen({ kind: "purge", tenant: t })}>
              Purge…
            </button>
          ) : null}
          {t.status === "offboarded" && t.may_restore ? <span className="text-xs text-muted">Purge from {date(t.purge_after)}</span> : null}
        </div>
      ),
    },
  ];

  return (
    <div>
      <PageHeader
        title="Manage customers"
        subtitle={
          admin
            ? "Rename a customer, change its short code, or delete it. Deleting offboards it at once; it can be restored during the retention period and purged after it."
            : "Rename a customer or change its short code. Deleting a customer is a platform administrator's."
        }
      />
      <div className="space-y-6">
        <Feedback message={message} />
        {open?.kind === "edit" ? <EditForm key={open.tenant.tenant.id} tenant={open.tenant} onDone={done} onCancel={() => setOpen(null)} /> : null}
        {open?.kind === "offboard" ? <OffboardForm key={open.tenant.tenant.id} tenant={open.tenant} onDone={done} onCancel={() => setOpen(null)} /> : null}
        {open?.kind === "purge" ? <PurgeForm key={open.tenant.tenant.id} tenant={open.tenant} onDone={done} onCancel={() => setOpen(null)} /> : null}

        <section aria-labelledby="live-heading">
          <h2 id="live-heading" className="mb-2 section-title">
            Customers
          </h2>
          <DataTable
            id="manage-customers"
            caption="Customers"
            rows={live.data}
            rowKey={(t) => t.tenant.id}
            columns={columns}
            role={me.role}
            empty={{ title: "No customers in your scope", next: "Onboard a customer in Customer mapping, or ask a platform administrator to assign customers to you." }}
          />
        </section>

        {admin ? (
          <section aria-labelledby="gone-heading">
            <h2 id="gone-heading" className="mb-2 section-title">
              Deleted customers
            </h2>
            {gone.status === "loading" ? <Loading what="deleted customers" /> : null}
            {gone.status === "error" ? <LoadError error={gone.error} what="deleted customers" /> : null}
            {gone.status === "ready" ? (
              gone.data.length === 0 ? (
                <EmptyState title="No deleted customers" next="A customer you delete is listed here while it can be restored, and after it is purged." />
              ) : (
                <DataTable
                  id="deleted-customers"
                  caption="Deleted customers"
                  rows={gone.data}
                  rowKey={(t) => t.tenant.id}
                  columns={goneColumns}
                  role={me.role}
                  empty={{ title: "No deleted customers", next: "A customer you delete is listed here." }}
                />
              )
            ) : null}
          </section>
        ) : null}
      </div>
    </div>
  );
}
