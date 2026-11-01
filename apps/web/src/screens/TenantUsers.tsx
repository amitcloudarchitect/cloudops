"use client";

import { useState } from "react";
import type { FormEvent } from "react";

import type { RoleOption, TenantUser } from "@cloudops/contracts";

import { DataTable, type Column } from "@/components/DataTable";
import { LoadError, Loading } from "@/components/Loading";
import { PageHeader } from "@/components/PageHeader";
import { ApiError, apiGet, apiPost } from "@/lib/api";
import { useAssistantContext } from "@/lib/assistant";
import { useAuth } from "@/lib/auth";
import { date } from "@/lib/format";
import { useMe } from "@/lib/session";
import { useApi } from "@/lib/useApi";

const control = "w-full field";
const label = "flex flex-col gap-1 text-xs font-semibold text-muted";
const primary = "btn btn-primary";
const secondary = "btn btn-secondary";
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

type Message = { kind: "ok" | "error"; text: string } | null;

function failure(error: unknown, what: string): string {
  if (error instanceof ApiError) {
    if (error.code === "last_administrator") return `${what} was not saved: your organisation needs at least one administrator.`;
    if (error.status === 404) return `${what} was not saved: there is no such user in your organisation.`;
    if (error.status === 422) return `${what} was not saved: choose one of your organisation’s agreed roles, and check the object id.`;
    if (error.status === 403) return `${what} was not saved: you are not an administrator of your organisation.`;
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

function UserForm({
  roles,
  editing,
  onSaved,
  onCancel,
}: {
  roles: RoleOption[];
  editing: TenantUser | null;
  onSaved: (users: TenantUser[], text: string) => void;
  onCancel: () => void;
}) {
  const { token } = useAuth();
  const [oid, setOid] = useState(editing?.oid ?? "");
  const [name, setName] = useState(editing?.display_name ?? "");
  const [role, setRole] = useState(editing?.role ?? "");
  const [admin, setAdmin] = useState(editing?.tenant_admin ?? false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<Message>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setMessage(null);
    if (!UUID.test(oid.trim()) || !name.trim() || !role) {
      setMessage({ kind: "error", text: "Give the user’s object id, a name, and a role." });
      return;
    }
    setBusy(true);
    try {
      const users = await apiPost(await token(), "/v1/tenant/users", {}, { oid: oid.trim(), display_name: name.trim(), role, tenant_admin: admin });
      onSaved(users, editing ? `${name.trim()} updated.` : `${name.trim()} added. They can sign in now.`);
    } catch (error) {
      setMessage({ kind: "error", text: failure(error, editing ? "The change" : "The user") });
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} aria-labelledby="user-heading" className="space-y-3 card p-4">
      <h2 id="user-heading" className="text-base font-semibold text-ink">
        {editing ? `Change ${editing.display_name}` : "Add a user"}
      </h2>
      <p className="text-sm text-muted">
        The user signs in with their own organisation account. Their object id is shown on the sign-in page when they have no access yet.
      </p>
      <div className="grid gap-3 sm:grid-cols-2">
        <label className={label}>
          Object id
          <input className={control} value={oid} onChange={(e) => setOid(e.target.value)} readOnly={editing !== null} required spellCheck={false} />
        </label>
        <label className={label}>
          Name
          <input className={control} value={name} onChange={(e) => setName(e.target.value)} maxLength={200} required />
        </label>
        <label className={label}>
          Role
          <select className={control} value={role} onChange={(e) => setRole(e.target.value)} required>
            <option value="">Choose…</option>
            {roles.map((r) => (
              <option key={r.name} value={r.name}>
                {r.name}
                {r.description ? ` — ${r.description}` : ""}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-2 self-end text-sm text-ink">
          <input type="checkbox" checked={admin} onChange={(e) => setAdmin(e.target.checked)} />
          Administers your organisation’s users
        </label>
      </div>
      <Feedback message={message} />
      <div className="flex gap-2">
        <button type="submit" disabled={busy} className={primary}>
          {editing ? "Save" : "Add user"}
        </button>
        <button type="button" onClick={onCancel} className={secondary}>
          Cancel
        </button>
      </div>
    </form>
  );
}

// A customer administrator's own users (ADR 0018, D2). Getronics does not
// administer customer user lists; the database confines this to the caller's
// organisation and to the roles agreed with it.
export function TenantUsers() {
  useAssistantContext({ screen: "users" });
  const me = useMe();
  const { token } = useAuth();
  const loaded = useApi(async (t) => {
    const [users, roles] = await Promise.all([apiGet(t, "/v1/tenant/users"), apiGet(t, "/v1/tenant/roles")]);
    return { users, roles };
  }, "tenant-users");
  const [users, setUsers] = useState<TenantUser[] | null>(null);
  const [form, setForm] = useState<{ editing: TenantUser | null } | null>(null);
  const [message, setMessage] = useState<Message>(null);

  if (loaded.status === "loading") return <Loading what="your organisation’s users" />;
  if (loaded.status === "error") return <LoadError error={loaded.error} what="users" />;
  const shown = users ?? loaded.data.users;

  async function disable(user: TenantUser) {
    if (!window.confirm(`Remove ${user.display_name}'s access? They are signed out on their next request.`)) return;
    setMessage(null);
    try {
      setUsers(await apiPost(await token(), "/v1/tenant/users/{oid}/disable", { oid: user.oid }, undefined));
      setMessage({ kind: "ok", text: `${user.display_name} no longer has access.` });
    } catch (error) {
      setMessage({ kind: "error", text: failure(error, "The removal") });
    }
  }

  const columns: Column<TenantUser>[] = [
    { key: "name", header: "Name", value: (u) => u.display_name },
    { key: "role", header: "Role", value: (u) => u.role },
    { key: "admin", header: "Administrator", value: (u) => (u.tenant_admin ? "Yes" : "No") },
    { key: "limited", header: "Subscriptions", value: (u) => (u.subscription_limited ? "Some" : "All") },
    { key: "status", header: "Access", value: (u) => (u.disabled_at ? `Removed ${date(u.disabled_at)}` : "Active") },
    { key: "oid", header: "Object id", value: (u) => u.oid, className: "font-mono text-xs" },
    {
      key: "actions",
      header: "Actions",
      value: () => null,
      render: (u) =>
        u.disabled_at ? (
          <button type="button" className={secondary} onClick={() => setForm({ editing: u })} aria-label={`Restore ${u.display_name}`}>
            Restore
          </button>
        ) : (
          <div className="flex gap-2">
            <button type="button" className={secondary} onClick={() => setForm({ editing: u })} aria-label={`Change ${u.display_name}`}>
              Change
            </button>
            {u.oid === me.oid ? null : (
              <button type="button" className={secondary} onClick={() => void disable(u)} aria-label={`Remove ${u.display_name}`}>
                Remove
              </button>
            )}
          </div>
        ),
    },
  ];

  return (
    <>
      <PageHeader title="Users" subtitle="Who in your organisation can use the portal, and with which role.">
        {form ? null : (
          <button type="button" className={primary} onClick={() => setForm({ editing: null })}>
            Add a user
          </button>
        )}
      </PageHeader>
      <div className="space-y-4">
        <Feedback message={message} />
        {form ? (
          <UserForm
            key={form.editing?.oid ?? "new"}
            roles={loaded.data.roles}
            editing={form.editing}
            onSaved={(next, text) => {
              setUsers(next);
              setForm(null);
              setMessage({ kind: "ok", text });
            }}
            onCancel={() => setForm(null)}
          />
        ) : null}
        <DataTable
          id="tenant-users"
          caption="Your organisation’s users"
          rows={shown}
          rowKey={(u) => u.oid}
          columns={columns}
          facets={[{ key: "role", label: "Role", value: (u) => u.role }]}
          role={me.role}
          empty={{ title: "No users yet", next: "Add the first user with their object id." }}
        />
      </div>
    </>
  );
}
