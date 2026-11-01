"use client";

import { useState } from "react";
import type { FormEvent, ReactNode } from "react";

import type { CatalogueRole, FederationRequest, Onboarding } from "@cloudops/contracts";

import { DataTable, type Column } from "@/components/DataTable";
import { HealthBadge } from "@/components/HealthBadge";
import { LoadError, Loading } from "@/components/Loading";
import { PageHeader } from "@/components/PageHeader";
import { ApiError, apiGet, apiPost } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { date } from "@/lib/format";
import type { Band } from "@/lib/health";
import { useMe } from "@/lib/session";
import { useApi } from "@/lib/useApi";

const control = "w-full field";
const label = "flex flex-col gap-1 text-xs font-semibold text-muted";
const primary = "btn btn-primary";
const secondary = "btn btn-secondary";
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

const STATUS: Record<Onboarding["status"], { band: Band; label: string }> = {
  not_started: { band: "unknown", label: "Not started" },
  setup: { band: "watch", label: "Setting up" },
  live: { band: "healthy", label: "Live" },
  suspended: { band: "critical", label: "Suspended" },
};
const IDP_LABEL: Record<FederationRequest["idp_kind"], string> = {
  entra: "Microsoft Entra ID (B2B collaboration)",
  saml: "SAML 2.0 identity provider (direct federation)",
  wsfed: "WS-Federation identity provider (direct federation)",
};

type Message = { kind: "ok" | "error"; text: string } | null;

function failure(error: unknown, what: string): string {
  if (error instanceof ApiError) {
    if (error.code === "step_out_of_order") return `${what} was not saved: complete the earlier steps first.`;
    if (error.status === 409) return `${what} was not saved: it is already done.`;
    if (error.status === 404) return `${what} was not saved: the customer is not in your scope.`;
    if (error.status === 422) return `${what} was not saved: check the values.`;
  }
  return `${what} was not saved. Try again in a moment.`;
}

function Feedback({ message }: { message: Message }) {
  if (!message) return null;
  return (
    <p role={message.kind === "error" ? "alert" : "status"} className={`text-sm ${message.kind === "error" ? "text-band-critical" : "text-band-healthy"}`}>
      {message.text}
    </p>
  );
}

function Step({ n, title, done, children }: { n: number; title: string; done: string | null; children: ReactNode }) {
  return (
    <section aria-labelledby={`step-${n}`} className="space-y-3 card p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 id={`step-${n}`} className="text-base font-semibold text-ink">
          {n}. {title}
        </h3>
        {done ? <HealthBadge band="healthy" label={done} /> : <HealthBadge band="unknown" label="To do" />}
      </div>
      {children}
    </section>
  );
}

function Steps({ item, catalogue, onChanged }: { item: Onboarding; catalogue: CatalogueRole[]; onChanged: (next: Onboarding, text: string) => void }) {
  const { token } = useAuth();
  const tenant = { tenant_id: item.tenant.id };
  const [idp, setIdp] = useState<FederationRequest["idp_kind"]>(item.idp_kind ?? "entra");
  const [reference, setReference] = useState(item.idp_reference ?? "");
  const [domains, setDomains] = useState(item.domains.join(", "));
  const [roles, setRoles] = useState<string[]>(item.agreed_roles);
  const [agreement, setAgreement] = useState(item.roles_agreement_ref ?? "");
  const [adminOid, setAdminOid] = useState("");
  const [adminName, setAdminName] = useState("");
  const [adminRole, setAdminRole] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<Message>(null);

  async function run(what: string, call: (t: string) => Promise<Onboarding>, done: string) {
    setBusy(true);
    setMessage(null);
    try {
      onChanged(await call(await token()), done);
    } catch (error) {
      setMessage({ kind: "error", text: failure(error, what) });
    } finally {
      setBusy(false);
    }
  }

  function federate(event: FormEvent) {
    event.preventDefault();
    const list = domains.split(/[\s,]+/).map((d) => d.trim().toLowerCase()).filter(Boolean);
    if (!reference.trim() || list.length === 0) {
      setMessage({ kind: "error", text: "Give the identity provider reference and at least one domain." });
      return;
    }
    void run("Federation", (t) => apiPost(t, "/v1/onboarding/{tenant_id}/federation", tenant, { idp_kind: idp, idp_reference: reference.trim(), domains: list }), "Federation recorded.");
  }

  function agree(event: FormEvent) {
    event.preventDefault();
    if (roles.length === 0 || !agreement.trim()) {
      setMessage({ kind: "error", text: "Choose the agreed roles and say where the agreement is recorded." });
      return;
    }
    void run("The role set", (t) => apiPost(t, "/v1/onboarding/{tenant_id}/roles", tenant, { roles, agreement_ref: agreement.trim() }), "Role set agreed.");
  }

  function assign(event: FormEvent) {
    event.preventDefault();
    if (!UUID.test(adminOid.trim()) || !adminName.trim() || !adminRole) {
      setMessage({ kind: "error", text: "Give the administrator’s object id in the Getronics directory, a name and a role." });
      return;
    }
    void run(
      "The first administrator",
      (t) => apiPost(t, "/v1/onboarding/{tenant_id}/first-admin", tenant, { oid: adminOid.trim(), display_name: adminName.trim(), role: adminRole }),
      "First administrator assigned.",
    );
  }

  const ready = !!item.federation_recorded_at && !!item.roles_agreed_at && !!item.first_admin_oid;
  return (
    <div className="space-y-4">
      <Feedback message={message} />
      <Step n={1} title="Federation" done={item.federation_recorded_at ? `Recorded ${date(item.federation_recorded_at)}` : null}>
        <p className="text-sm text-muted">Set up in Entra first (docs/customer-portal-federation.md), then record it here.</p>
        <form onSubmit={federate} className="grid gap-3 sm:grid-cols-2">
          <label className={label}>
            Identity provider
            <select className={control} value={idp} onChange={(e) => setIdp(e.target.value as FederationRequest["idp_kind"])}>
              {Object.entries(IDP_LABEL).map(([value, text]) => (
                <option key={value} value={value}>
                  {text}
                </option>
              ))}
            </select>
          </label>
          <label className={label}>
            {idp === "entra" ? "Customer’s Entra tenant id" : "Identity provider issuer URI"}
            <input className={control} value={reference} onChange={(e) => setReference(e.target.value)} maxLength={300} spellCheck={false} />
          </label>
          <label className={`${label} sm:col-span-2`}>
            Verified domains, comma separated
            <input className={control} value={domains} onChange={(e) => setDomains(e.target.value)} spellCheck={false} placeholder="example.com" />
          </label>
          <div>
            <button type="submit" disabled={busy} className={secondary}>
              Record federation
            </button>
          </div>
        </form>
      </Step>
      <Step n={2} title="Role set agreed with the customer" done={item.roles_agreed_at ? `Agreed ${date(item.roles_agreed_at)}` : null}>
        <form onSubmit={agree} className="space-y-3">
          <fieldset className="grid gap-2 sm:grid-cols-2">
            <legend className="mb-1 text-xs font-medium text-muted">Roles the customer may use</legend>
            {catalogue.map((r) => (
              <label key={r.name} className="flex items-start gap-2 text-sm text-ink">
                <input
                  type="checkbox"
                  className="mt-1"
                  checked={roles.includes(r.name)}
                  onChange={(e) => setRoles((now) => (e.target.checked ? [...now, r.name] : now.filter((x) => x !== r.name)))}
                />
                <span>
                  <span className="font-medium">{r.name}</span>
                  {r.description ? <span className="block text-xs text-muted">{r.description}</span> : null}
                </span>
              </label>
            ))}
          </fieldset>
          <label className={label}>
            Where the agreement is recorded
            <input className={control} value={agreement} onChange={(e) => setAgreement(e.target.value)} maxLength={200} placeholder="e.g. SOW-114 schedule 3" />
          </label>
          <button type="submit" disabled={busy} className={secondary}>
            Record the role set
          </button>
        </form>
      </Step>
      <Step n={3} title="First customer administrator" done={item.first_admin_at ? `Assigned ${date(item.first_admin_at)}` : null}>
        {item.first_admin_oid ? (
          <p className="text-sm text-muted">
            Object id <code className="font-mono text-ink">{item.first_admin_oid}</code>. The customer manages its other users from here on.
          </p>
        ) : (
          <form onSubmit={assign} className="grid gap-3 sm:grid-cols-3">
            <label className={label}>
              Object id (B2B guest)
              <input className={control} value={adminOid} onChange={(e) => setAdminOid(e.target.value)} spellCheck={false} />
            </label>
            <label className={label}>
              Name
              <input className={control} value={adminName} onChange={(e) => setAdminName(e.target.value)} maxLength={200} />
            </label>
            <label className={label}>
              Role
              <select className={control} value={adminRole} onChange={(e) => setAdminRole(e.target.value)}>
                <option value="">Choose…</option>
                {item.agreed_roles.map((r) => (
                  <option key={r} value={r}>
                    {r}
                  </option>
                ))}
              </select>
            </label>
            <div>
              <button type="submit" disabled={busy} className={secondary}>
                Assign administrator
              </button>
            </div>
          </form>
        )}
      </Step>
      <Step n={4} title="Go live" done={item.status === "live" && item.live_at ? `Live since ${date(item.live_at)}` : null}>
        <p className="text-sm text-muted">
          Until the portal is live, none of the customer’s users can sign in. Suspending stops them all on their next request and keeps their assignments.
        </p>
        {item.status === "live" ? (
          <button type="button" disabled={busy} className={secondary} onClick={() => void run("Suspension", (t) => apiPost(t, "/v1/onboarding/{tenant_id}/live", tenant, { live: false }), "Portal suspended.")}>
            Suspend portal access
          </button>
        ) : (
          <button type="button" disabled={busy || !ready} className={primary} onClick={() => void run("Go-live", (t) => apiPost(t, "/v1/onboarding/{tenant_id}/live", tenant, { live: true }), "Portal live.")}>
            Go live
          </button>
        )}
      </Step>
    </div>
  );
}

// AMARA for the customer's portal users (ADR 0029): off until switched on here.
// Getronics staff always have it. The API does not report the current setting, so
// the switch shows what was last set from this page, and off before that.
function AssistantSwitch({ tenantId, enabled, onChanged }: { tenantId: string; enabled: boolean; onChanged: (enabled: boolean) => void }) {
  const { token } = useAuth();
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<Message>(null);

  async function set(next: boolean) {
    setBusy(true);
    setMessage(null);
    try {
      const view = await apiPost(await token(), "/v1/assistant/tenants/{tenant_id}/enabled", { tenant_id: tenantId }, { enabled: next });
      onChanged(view.enabled);
      setMessage({ kind: "ok", text: view.enabled ? "AMARA is on for this customer’s portal users." : "AMARA is off for this customer’s portal users." });
    } catch (error) {
      setMessage({ kind: "error", text: failure(error, "The AMARA setting") });
    } finally {
      setBusy(false);
    }
  }

  return (
    <section aria-labelledby="assistant-switch" className="space-y-2 card p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h3 id="assistant-switch" className="text-base font-semibold text-ink">
            AMARA assistant for portal users
          </h3>
          <p id="assistant-switch-hint" className="text-sm text-muted">
            Read-only answers about this customer’s own account. Off by default; Getronics staff always have it.
          </p>
        </div>
        <button
          type="button"
          role="switch"
          aria-checked={enabled}
          aria-labelledby="assistant-switch"
          aria-describedby="assistant-switch-hint"
          disabled={busy}
          onClick={() => void set(!enabled)}
          className="inline-flex items-center gap-2 rounded-control text-sm font-semibold text-ink disabled:cursor-not-allowed disabled:opacity-55"
        >
          <span
            aria-hidden="true"
            className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full border transition-colors ${enabled ? "border-accent bg-accent" : "border-line bg-surface-raised"}`}
          >
            <span className={`inline-block size-4 rounded-full bg-ink transition-transform ${enabled ? "translate-x-6" : "translate-x-1"}`} />
          </span>
          {enabled ? "On" : "Off"}
        </button>
      </div>
      <Feedback message={message} />
    </section>
  );
}

// Portal onboarding (HLD M2, ADR 0018): a platform administrator records the
// customer's federation, the role set agreed with it and its first
// administrator, then opens the portal. The database enforces the order.
export function PortalOnboarding() {
  const me = useMe();
  const loaded = useApi(async (t) => {
    const [items, catalogue] = await Promise.all([apiGet(t, "/v1/onboarding"), apiGet(t, "/v1/onboarding/roles")]);
    return { items, catalogue };
  }, "onboarding");
  const [items, setItems] = useState<Onboarding[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [message, setMessage] = useState<Message>(null);
  const [assistant, setAssistant] = useState<Record<string, boolean>>({});

  if (loaded.status === "loading") return <Loading what="portal onboarding" />;
  if (loaded.status === "error") return <LoadError error={loaded.error} what="portal onboarding" />;
  const shown = items ?? loaded.data.items;
  const current = shown.find((o) => o.tenant.id === selected) ?? null;

  const columns: Column<Onboarding>[] = [
    {
      key: "customer",
      header: "Customer",
      value: (o) => o.tenant.name,
      render: (o) => (
        <button type="button" className="link font-medium" onClick={() => setSelected(o.tenant.id)}>
          {o.tenant.name}
        </button>
      ),
    },
    { key: "status", header: "Portal", value: (o) => STATUS[o.status].label, render: (o) => <HealthBadge band={STATUS[o.status].band} label={STATUS[o.status].label} /> },
    { key: "idp", header: "Identity provider", value: (o) => (o.idp_kind ? IDP_LABEL[o.idp_kind] : null) },
    { key: "domains", header: "Domains", value: (o) => o.domains.join(", ") || null },
    { key: "roles", header: "Agreed roles", value: (o) => (o.agreed_roles.length ? String(o.agreed_roles.length) : null) },
    { key: "live", header: "Live since", value: (o) => o.live_at, render: (o) => (o.status === "live" ? date(o.live_at) : "—") },
  ];

  return (
    <>
      <PageHeader title="Portal onboarding" subtitle="Open the customer portal to a customer: federation, agreed roles, its first administrator, then go-live." />
      <div className="space-y-4">
        <Feedback message={message} />
        <DataTable
          id="portal-onboarding"
          caption="Customers and their portal"
          rows={shown}
          rowKey={(o) => o.tenant.id}
          columns={columns}
          facets={[{ key: "status", label: "Portal", value: (o) => STATUS[o.status].label }]}
          role={me.role}
          empty={{ title: "No customers in your scope", next: "Customers are onboarded in Customer mapping first." }}
        />
        {current ? (
          <section aria-labelledby="onboarding-customer" className="space-y-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h2 id="onboarding-customer" className="section-title">
                {current.tenant.name}
              </h2>
              <button type="button" className={secondary} onClick={() => setSelected(null)}>
                Close
              </button>
            </div>
            <AssistantSwitch
              key={`assistant:${current.tenant.id}`}
              tenantId={current.tenant.id}
              enabled={assistant[current.tenant.id] ?? current.assistant_enabled}
              onChanged={(enabled) => setAssistant((now) => ({ ...now, [current.tenant.id]: enabled }))}
            />
            <Steps
              key={`${current.tenant.id}:${current.status}:${current.agreed_roles.join(",")}`}
              item={current}
              catalogue={loaded.data.catalogue}
              onChanged={(next, text) => {
                setItems(shown.map((o) => (o.tenant.id === next.tenant.id ? next : o)));
                setMessage({ kind: "ok", text: `${next.tenant.name}: ${text}` });
              }}
            />
          </section>
        ) : null}
      </div>
    </>
  );
}
