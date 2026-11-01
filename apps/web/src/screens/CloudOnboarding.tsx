"use client";

import { useEffect, useState } from "react";
import type { FormEvent } from "react";

import type { CloudCredential, TenantAliases } from "@cloudops/contracts";

import { DataTable, type Column } from "@/components/DataTable";
import { HealthBadge } from "@/components/HealthBadge";
import { LoadError, Loading } from "@/components/Loading";
import { PageHeader } from "@/components/PageHeader";
import { ApiError, apiGet, apiPost } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { date, dateTime } from "@/lib/format";
import type { Band } from "@/lib/health";
import { useMe } from "@/lib/session";
import { useApi } from "@/lib/useApi";

const control = "w-full field";
const label = "flex flex-col gap-1 text-xs font-semibold text-muted";
const primary = "btn btn-primary";
const secondary = "btn btn-secondary";
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const ACCOUNT = /^[0-9]{12}$/;
const ACCESS_KEY = /^AKIA[A-Z0-9]{16}$/;
const CODE = /^[A-Z0-9]{2,10}$/;
const MAX_DAYS = 90; // ADR 0003, guardrail 3
// The latest day to pick: expiry is recorded as the end of the chosen day, so day 90
// would fall after now + 90 days and the database refuses it.
const LAST_DAY = MAX_DAYS - 1;
const POLL_MS = 15_000;
// Where the secrets go: the platform's application Key Vault, per environment (LLD naming).
const KEY_VAULT = "kv-cloudops-app-<env>-weu (dev: kv-cloudops-app-dev-weu)";

type Provider = CloudCredential["provider"];
type Message = { kind: "ok" | "error"; text: string } | null;

const STATUS: Record<CloudCredential["status"], { band: Band; label: string }> = {
  pending: { band: "watch", label: "Waiting for its check" },
  active: { band: "healthy", label: "Active" },
  refused: { band: "critical", label: "Refused" },
  revoked: { band: "unknown", label: "Revoked" },
};

// The check's codes (cloudops_connectors.customer.check), in words.
const OUTCOME: Record<string, string> = {
  read_only: "Read-only, as ADR 0003 requires. The first sync has started.",
  secret_not_in_key_vault: "The secret is not in Key Vault yet, or the worker has not mounted it. Create it, then check again.",
  secret_expired: "The secret has expired. Rotate it, then record the new expiry.",
  credential_rejected: "The cloud rejected the secret or key.",
  directory_mismatch: "The token is from a different Entra tenant than the one recorded.",
  client_mismatch: "The token is for a different app registration than the one recorded.",
  subscription_not_visible: "The app registration cannot see every recorded subscription.",
  scope_wider_than_tenant: "The app registration can see subscriptions beyond this customer's. Remove those role assignments: one credential reaches one customer.",
  role_not_allowed: "The app registration holds a role beyond Reader, Cost Management Reader and Monitoring Reader. Remove it.",
  reader_missing: "Reader is missing on a subscription.",
  cannot_read_role_assignments: "The app registration cannot read its own role assignments: Reader is missing.",
  account_held_by_another_tenant: "Another tenant already holds one of these accounts: a subscription belongs to one tenant only.",
  access_key_mismatch: "The access key id in Key Vault is not the one recorded.",
  account_mismatch: "The key belongs to a different AWS account.",
  not_an_iam_user: "The key is not an IAM user's (never use the root user's keys).",
  inline_policy: "The IAM user or its group has an inline policy. Remove it.",
  policy_not_allowed: "The IAM user holds a policy beyond ReadOnlyAccess and Cost Explorer read. Remove it.",
  read_only_access_missing: "ReadOnlyAccess is not attached.",
  cannot_read_own_policies: "The key cannot read its own IAM policies: ReadOnlyAccess is missing.",
  cloud_unreachable: "The cloud did not answer. Check again in a moment.",
  cloud_error: "The cloud returned an error. Check again in a moment.",
  revoked: "The credential is revoked.",
  internal_error: "The check failed on our side. Check again; if it repeats, look at the worker's log.",
};

function failure(error: unknown, what: string): string {
  if (error instanceof ApiError) {
    if (error.status === 409) return `${what} was not saved: this customer already has a live credential for that cloud, or a check is already waiting.`;
    if (error.status === 404) return `${what} was not saved: the customer is not in your scope.`;
    if (error.status === 422) return `${what} was not saved: check the values. The secret must expire within ${MAX_DAYS} days.`;
    if (error.status === 403) return `${what} was not saved: you are not permitted.`;
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

function isoDay(offsetDays: number): string {
  const d = new Date();
  d.setUTCDate(d.getUTCDate() + offsetDays);
  return d.toISOString().slice(0, 10);
}

/** The end of the chosen day, UTC: what the secret's expiry is recorded as. */
function endOfDay(day: string): string {
  return `${day}T23:59:59Z`;
}

function SecretNames({ item }: { item: CloudCredential }) {
  return (
    <div className="space-y-2">
      <p className="text-sm text-ink">
        Create {item.secret_names.length === 1 ? "this secret" : "these secrets"} in Key Vault {KEY_VAULT}, out of band. Never paste a secret here, in a ticket or in chat.
      </p>
      <ul className="space-y-1">
        {item.secret_names.map((name, i) => (
          <li key={name} className="text-sm">
            <code className="rounded bg-surface-raised px-1 py-0.5">{name}</code>
            <span className="text-muted">
              {" "}
              {item.provider === "azure" ? "— the app registration's client secret value" : i === 0 ? "— the access key id" : "— the secret access key"}
            </span>
          </li>
        ))}
      </ul>
      <p className="text-xs text-muted">
        In Azure the worker mounts the secret through its SecretProviderClass, so the name must also be listed there (docs/cloud-onboarding.md). Locally it is the{" "}
        <code>{(item.secret_names[0] ?? "").toUpperCase().replaceAll("-", "_")}</code> line in .env.local.
      </p>
    </div>
  );
}

function Detail({ item, onChanged }: { item: CloudCredential; onChanged: (next: CloudCredential, text: string) => void }) {
  const { token } = useAuth();
  const [expiry, setExpiry] = useState(isoDay(LAST_DAY));
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<Message>(null);
  const params = { credential_id: item.id };
  const check = item.latest_check;
  const waiting = check?.status === "requested" || check?.status === "running";

  async function run(what: string, call: (t: string) => Promise<CloudCredential>, done: string) {
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

  function rotate(event: FormEvent) {
    event.preventDefault();
    void run("The rotation", (t) => apiPost(t, "/v1/cloud-onboarding/credentials/{credential_id}/checks", params, { secret_expires_at: endOfDay(expiry) }), "Rotation recorded; the new secret is being checked.");
  }

  return (
    <div className="space-y-4">
      <Feedback message={message} />
      <section className="card space-y-3 p-4" aria-labelledby="credential-secrets">
        <h3 id="credential-secrets" className="text-base font-semibold text-ink">
          Key Vault
        </h3>
        <SecretNames item={item} />
      </section>
      <section className="card space-y-2 p-4" aria-labelledby="credential-check">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h3 id="credential-check" className="text-base font-semibold text-ink">
            Read-only check
          </h3>
          {check ? <HealthBadge band={check.status === "passed" ? "healthy" : check.status === "refused" ? "critical" : "watch"} label={check.status} /> : null}
        </div>
        {check ? (
          <>
            <p className="text-sm text-ink">
              {waiting ? "Waiting for the connector worker (within a minute of the secret being mounted)." : OUTCOME[check.outcome ?? ""] ?? check.outcome}
            </p>
            <p className="text-xs text-muted">
              Asked {dateTime(check.requested_at)} ({check.reason}){check.finished_at ? `, finished ${dateTime(check.finished_at)}` : ""}.
            </p>
            {check.detail ? (
              <pre className="overflow-x-auto rounded bg-surface-raised p-2 text-xs" aria-label="What the check found">
                {JSON.stringify(check.detail, null, 2)}
              </pre>
            ) : null}
          </>
        ) : (
          <p className="text-sm text-muted">No check yet.</p>
        )}
      </section>
      {item.status !== "revoked" ? (
        <section className="card space-y-3 p-4" aria-labelledby="credential-actions">
          <h3 id="credential-actions" className="text-base font-semibold text-ink">
            Actions
          </h3>
          <div className="flex flex-wrap gap-2">
            <button type="button" disabled={busy || waiting} className={secondary} onClick={() => void run("The check", (t) => apiPost(t, "/v1/cloud-onboarding/credentials/{credential_id}/checks", params, {}), "Check requested.")}>
              Check again
            </button>
            <button
              type="button"
              disabled={busy}
              className={secondary}
              onClick={() => {
                if (window.confirm("Revoke this credential? The platform stops using it at once. Delete the Key Vault secret and the cloud-side secret or key afterwards.")) {
                  void run("The revocation", (t) => apiPost(t, "/v1/cloud-onboarding/credentials/{credential_id}/revoke", params, undefined), "Revoked. Now delete the secret in Key Vault and in the cloud.");
                }
              }}
            >
              Revoke
            </button>
          </div>
          <form onSubmit={rotate} className="flex flex-wrap items-end gap-2">
            <label className={label}>
              Rotated: the new secret expires on
              <input type="date" className={control} value={expiry} min={isoDay(1)} max={isoDay(LAST_DAY)} onChange={(e) => setExpiry(e.target.value)} />
            </label>
            <button type="submit" disabled={busy || waiting} className={secondary}>
              Record rotation
            </button>
          </form>
        </section>
      ) : null}
    </div>
  );
}

const HELD: Record<string, [string, string]> = { azure: ["Azure subscription", "Azure subscriptions"], aws: ["AWS account", "AWS accounts"] };

/** "Azure Sponsorship (AZSPONSOR) · 1 Azure subscription": a customer may hold several
 * accounts, so the list says what each already holds. */
function tenantLabel(t: TenantAliases): string {
  const held = Object.entries(t.accounts)
    .filter(([, n]) => n > 0)
    .map(([provider, n]) => {
      const [one, many] = HELD[provider] ?? [`${provider} account`, `${provider} accounts`];
      return `${n} ${n === 1 ? one : many}`;
    });
  const name = `${t.tenant.name} (${t.short_code})`;
  return held.length ? `${name} · ${held.join(", ")}` : name;
}

function OnboardForm({ tenants, onDone }: { tenants: TenantAliases[]; onDone: (item: CloudCredential) => void }) {
  const { token } = useAuth();
  const [tenantId, setTenantId] = useState("");
  const [newName, setNewName] = useState("");
  const [newCode, setNewCode] = useState("");
  const [newKind, setNewKind] = useState<"" | "customer" | "internal">("");
  const [provider, setProvider] = useState<Provider>("azure");
  const [directory, setDirectory] = useState("");
  const [client, setClient] = useState("");
  const [subscriptions, setSubscriptions] = useState("");
  const [account, setAccount] = useState("");
  const [keyId, setKeyId] = useState("");
  const [expiry, setExpiry] = useState(isoDay(LAST_DAY));
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<Message>(null);
  const creating = tenantId === "new";

  async function submit(event: FormEvent) {
    event.preventDefault();
    const subs = subscriptions.split(/[\s,]+/).map((s) => s.trim().toLowerCase()).filter(Boolean);
    const problems: string[] = [];
    if (!tenantId) problems.push("choose the customer");
    if (creating && (!newName.trim() || !CODE.test(newCode))) problems.push("give the new tenant a name and a short code of 2 to 10 capitals or digits");
    if (creating && newKind === "") problems.push("choose whether the new tenant is a customer or internal to Getronics");
    if (provider === "azure" && (!UUID.test(directory.trim()) || !UUID.test(client.trim()))) problems.push("give the directory (tenant) id and the application (client) id");
    if (provider === "azure" && (subs.length === 0 || subs.some((s) => !UUID.test(s)))) problems.push("give the subscription ids, one per line");
    if (provider === "aws" && (!ACCOUNT.test(account.trim()) || !ACCESS_KEY.test(keyId.trim()))) problems.push("give the twelve-digit account id and the access key id (AKIA…)");
    if (problems.length) {
      setMessage({ kind: "error", text: `Please ${problems.join("; ")}.` });
      return;
    }
    setBusy(true);
    setMessage(null);
    try {
      const t = await token();
      let tenant = tenantId;
      if (creating) tenant = (await apiPost(t, "/v1/mapping/tenants", {}, { canonical_name: newName.trim(), short_code: newCode, internal: newKind === "internal" })).tenant.id;
      const expires = endOfDay(expiry);
      const created =
        provider === "azure"
          ? await apiPost(t, "/v1/cloud-onboarding/credentials", {}, { provider, tenant_id: tenant, directory_id: directory.trim(), client_id: client.trim(), subscription_ids: subs, secret_expires_at: expires })
          : await apiPost(t, "/v1/cloud-onboarding/credentials", {}, { provider, tenant_id: tenant, account_id: account.trim(), access_key_id: keyId.trim(), secret_expires_at: expires });
      onDone(created);
    } catch (error) {
      setMessage({ kind: "error", text: failure(error, "The onboarding") });
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="card space-y-3 p-4" aria-labelledby="onboard-title">
      <h2 id="onboard-title" className="section-title">
        Onboard a cloud account
      </h2>
      <p className="text-sm text-muted">
        Identifiers only: the secret never comes here. Prepare the read-only identity first (docs/cloud-onboarding.md). Non-production only: production uses delegated access (ADR 0003).
      </p>
      <Feedback message={message} />
      <div className="grid gap-3 sm:grid-cols-2">
        <label className={label}>
          Customer
          <select className={control} value={tenantId} onChange={(e) => setTenantId(e.target.value)}>
            <option value="">Choose…</option>
            {tenants.map((t) => (
              <option key={t.tenant.id} value={t.tenant.id}>
                {tenantLabel(t)}
              </option>
            ))}
            <option value="new">A new tenant…</option>
          </select>
        </label>
        <fieldset className="flex flex-col gap-1">
          <legend className="text-xs font-semibold text-muted">Cloud</legend>
          <div className="flex gap-4 text-sm">
            {(["azure", "aws"] as const).map((p) => (
              <label key={p} className="flex items-center gap-1">
                <input type="radio" name="provider" checked={provider === p} onChange={() => setProvider(p)} />
                {p === "azure" ? "Azure (app registration)" : "AWS (IAM access key)"}
              </label>
            ))}
          </div>
        </fieldset>
        {creating ? (
          <>
            <label className={label}>
              New tenant’s name
              <input className={control} value={newName} onChange={(e) => setNewName(e.target.value)} maxLength={200} />
            </label>
            <label className={label}>
              Short code (names its Key Vault secrets)
              <input className={control} value={newCode} onChange={(e) => setNewCode(e.target.value.toUpperCase())} maxLength={10} />
            </label>
            <fieldset className="flex flex-col gap-1 sm:col-span-2">
              <legend className="text-xs font-semibold text-muted">Kind of tenant</legend>
              <div className="flex gap-4 text-sm">
                <label className="flex items-center gap-1">
                  <input type="radio" name="tenant-kind" checked={newKind === "customer"} onChange={() => setNewKind("customer")} />
                  A customer
                </label>
                <label className="flex items-center gap-1">
                  <input type="radio" name="tenant-kind" checked={newKind === "internal"} onChange={() => setNewKind("internal")} />
                  Internal to Getronics
                </label>
              </div>
              <p className="text-xs text-muted">
                Internal or not, a subscription belongs to one tenant only, and internal tenants are reported on like customers (ADR 0031). Change it later, or
                choose the tenant that hosts the platform, in Customer mapping.
              </p>
            </fieldset>
          </>
        ) : null}
        {provider === "azure" ? (
          <>
            <label className={label}>
              Directory (tenant) id
              <input className={control} value={directory} onChange={(e) => setDirectory(e.target.value)} />
            </label>
            <label className={label}>
              Application (client) id
              <input className={control} value={client} onChange={(e) => setClient(e.target.value)} />
            </label>
            <label className={`${label} sm:col-span-2`}>
              Subscription ids, one per line
              <textarea className={control} rows={3} value={subscriptions} onChange={(e) => setSubscriptions(e.target.value)} />
            </label>
          </>
        ) : (
          <>
            <label className={label}>
              AWS account id
              <input className={control} value={account} onChange={(e) => setAccount(e.target.value)} inputMode="numeric" />
            </label>
            <label className={label}>
              Access key id (not the secret key)
              <input className={control} value={keyId} onChange={(e) => setKeyId(e.target.value.trim())} />
            </label>
          </>
        )}
        <label className={label}>
          {provider === "azure" ? "The client secret expires on" : "The key will be rotated by"}
          <input type="date" className={control} value={expiry} min={isoDay(1)} max={isoDay(LAST_DAY)} onChange={(e) => setExpiry(e.target.value)} />
        </label>
      </div>
      <button type="submit" disabled={busy} className={primary}>
        Record and check
      </button>
    </form>
  );
}

// Cloud onboarding (ADR 0027, under ADR 0003): a platform administrator records a
// customer's app registration or access key by its identifiers, puts the secret in
// Key Vault out of band, and the connector worker checks it is read-only, binds its
// accounts to the customer and starts the first sync.
export function CloudOnboarding() {
  const me = useMe();
  const loaded = useApi(async (t) => {
    const [items, tenants] = await Promise.all([apiGet(t, "/v1/cloud-onboarding/credentials"), apiGet(t, "/v1/mapping/tenants")]);
    return { items, tenants };
  }, "cloud-onboarding");
  const [items, setItems] = useState<CloudCredential[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [message, setMessage] = useState<Message>(null);
  const { reload } = loaded;
  const fresh = loaded.status === "ready" ? loaded.data.items : null;
  const waiting = (items ?? fresh ?? []).some((c) => c.latest_check?.status === "requested" || c.latest_check?.status === "running");

  // A check runs in the worker, not here: poll while one is waiting.
  useEffect(() => {
    if (!waiting) return;
    const timer = window.setInterval(() => {
      setItems(null);
      reload();
    }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [waiting, reload]);

  if (loaded.status === "loading") return <Loading what="cloud onboarding" />;
  if (loaded.status === "error") return <LoadError error={loaded.error} what="cloud onboarding" />;
  const shown = items ?? loaded.data.items;
  const current = shown.find((c) => c.id === selected) ?? null;

  const columns: Column<CloudCredential>[] = [
    {
      key: "customer",
      header: "Customer",
      value: (c) => c.tenant.name,
      render: (c) => (
        <button type="button" className="link font-medium" onClick={() => setSelected(c.id)}>
          {c.tenant.name}
        </button>
      ),
    },
    { key: "cloud", header: "Cloud", value: (c) => (c.provider === "azure" ? "Azure" : "AWS") },
    { key: "accounts", header: "Accounts", value: (c) => c.account_ids.join(", ") },
    { key: "status", header: "Status", value: (c) => STATUS[c.status].label, render: (c) => <HealthBadge band={STATUS[c.status].band} label={STATUS[c.status].label} /> },
    { key: "check", header: "Last check", value: (c) => c.latest_check?.outcome ?? c.latest_check?.status ?? null },
    { key: "expires", header: "Secret expires", value: (c) => c.expires_at, render: (c) => `${date(c.expires_at)} (${c.days_to_expiry.value} days)` },
    { key: "owner", header: "Owner", value: (c) => (c.owned_by_me ? "You" : "Another administrator") },
  ];

  return (
    <>
      <PageHeader title="Cloud onboarding" subtitle="Read-only access to a customer’s Azure subscriptions or AWS account, with a stored credential (non-production only). The inventory of every stored credential." />
      <div className="space-y-4">
        <Feedback message={message} />
        <DataTable
          id="cloud-onboarding"
          caption="Stored customer credentials"
          rows={shown}
          rowKey={(c) => c.id}
          columns={columns}
          facets={[
            { key: "status", label: "Status", value: (c) => STATUS[c.status].label },
            { key: "cloud", label: "Cloud", value: (c) => (c.provider === "azure" ? "Azure" : "AWS") },
          ]}
          role={me.role}
          empty={{ title: "No stored credentials yet", next: "Onboard a cloud account below." }}
        />
        {current ? (
          <section aria-labelledby="credential-customer" className="space-y-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h2 id="credential-customer" className="section-title">
                {current.tenant.name} · {current.provider === "azure" ? "Azure" : "AWS"}
              </h2>
              <button type="button" className={secondary} onClick={() => setSelected(null)}>
                Close
              </button>
            </div>
            <Detail
              key={`${current.id}:${current.status}:${current.latest_check?.id ?? ""}`}
              item={current}
              onChanged={(next, text) => {
                setItems(shown.map((c) => (c.id === next.id ? next : c)));
                setMessage({ kind: "ok", text: `${next.tenant.name}: ${text}` });
              }}
            />
          </section>
        ) : null}
        <OnboardForm
          tenants={loaded.data.tenants}
          onDone={(created) => {
            setItems([created, ...shown]);
            setSelected(created.id);
            setMessage({ kind: "ok", text: `${created.tenant.name}: recorded. Create the secret in Key Vault; the check runs within a minute of it being mounted.` });
          }}
        />
      </div>
    </>
  );
}
