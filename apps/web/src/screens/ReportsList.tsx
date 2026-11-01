"use client";

// Monthly report packs (step 17, HLD M19): generate a customer's month, see each
// pack's status, and consolidate the portfolio in a reporting currency. A pack is
// in the customer's contract currency; the portfolio converts the same cost rows
// straight into the chosen currency, at each row's own rate.

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import type { PortfolioLine, PortfolioView, ReportPackSummary } from "@cloudops/contracts";

import { DataTable, type Column } from "@/components/DataTable";
import { HealthBadge } from "@/components/HealthBadge";
import { LoadError, Loading } from "@/components/Loading";
import { PageHeader } from "@/components/PageHeader";
import { Provenance } from "@/components/Provenance";
import { ApiError, apiGet, apiPost } from "@/lib/api";
import { useAssistantContext } from "@/lib/assistant";
import { useAuth } from "@/lib/auth";
import { count, date, money } from "@/lib/format";
import type { Band } from "@/lib/health";
import { can, useMe } from "@/lib/session";
import { useApi } from "@/lib/useApi";

export const STATUS: Record<ReportPackSummary["status"], { band: Band; label: string }> = {
  draft: { band: "watch", label: "Draft" },
  approved: { band: "at-risk", label: "Approved, not published" },
  published: { band: "healthy", label: "Published" },
  superseded: { band: "unknown", label: "Superseded" },
};
const CURRENCIES = ["EUR", "GBP", "USD"] as const;

function lastMonth(): string {
  const now = new Date();
  const d = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() - 1, 1));
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}`;
}

function Generate() {
  const me = useMe();
  const { token } = useAuth();
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (me.subscription_limited || me.tenants.length === 0) return null;

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setBusy(true);
    setError(null);
    try {
      const pack = await apiPost(await token(), "/v1/reports/packs", {}, { tenant_id: String(form.get("tenant")), period: String(form.get("period")) });
      router.push(`/reports/${pack.id}`);
    } catch (e) {
      setError(
        e instanceof ApiError && e.code === "contract_currency_not_captured"
          ? "No SOW is in force for that month, so the contract currency is not captured. Nothing was generated."
          : e instanceof ApiError && e.code === "period_in_future"
            ? "That month has not started yet."
            : "The pack was not generated. Try again in a moment.",
      );
      setBusy(false);
    }
  }

  return (
    <form onSubmit={(e) => void submit(e)} aria-label="Generate a monthly pack" className="mb-6 grid gap-3 card p-4 sm:grid-cols-3">
      <label className="text-sm text-ink">
        Customer
        <select name="tenant" required className="mt-1 block w-full field">
          {me.tenants.map((t) => (
            <option key={t.id} value={t.id}>
              {t.name}
            </option>
          ))}
        </select>
      </label>
      <label className="text-sm text-ink">
        Month
        <input name="period" type="month" required defaultValue={lastMonth()} className="mt-1 block w-full field" />
      </label>
      <div className="flex items-end">
        <button type="submit" disabled={busy} className="btn btn-primary">
          Generate pack
        </button>
      </div>
      {error ? (
        <p role="alert" className="text-sm text-band-critical sm:col-span-3">
          {error}
        </p>
      ) : null}
    </form>
  );
}

function Portfolio() {
  const me = useMe();
  const [period, setPeriod] = useState(lastMonth());
  const [currency, setCurrency] = useState<string>("EUR");
  const loaded = useApi((t) => apiGet(t, "/v1/reports/portfolio", {}, { period, currency }), `portfolio:${period}:${currency}`);
  const columns: Column<PortfolioLine>[] = [
    { key: "customer", header: "Customer", value: (l) => l.tenant.name, sortable: true },
    {
      key: "consumption",
      header: `Cloud cost (${currency})`,
      value: (l) => (l.consumption ? Number(l.consumption.value) : null),
      render: (l) => (l.consumption ? money(String(l.consumption.value), currency) : <span className="text-muted">Not shown: {l.gap}</span>),
      sortable: true,
      className: "tabular-nums",
    },
    { key: "incidents", header: "Incidents opened", value: (l) => l.incidents_opened.value, render: (l) => count(l.incidents_opened.value), sortable: true, className: "tabular-nums" },
    {
      key: "vulns",
      header: "Open severity 4 and 5",
      value: (l) => l.open_high_vulnerabilities.value,
      render: (l) => count(l.open_high_vulnerabilities.value),
      sortable: true,
      className: "tabular-nums",
    },
  ];
  return (
    <section aria-labelledby="portfolio-heading" className="mt-8">
      <h2 id="portfolio-heading" className="mb-2 section-title">
        Portfolio
      </h2>
      <div className="mb-3 flex flex-wrap gap-3">
        <label className="text-sm text-ink">
          Month
          <input type="month" value={period} onChange={(e) => setPeriod(e.target.value)} className="mt-1 block field" />
        </label>
        <label className="text-sm text-ink">
          Reporting currency
          <select value={currency} onChange={(e) => setCurrency(e.target.value)} className="mt-1 block field">
            {CURRENCIES.map((c) => (
              <option key={c}>{c}</option>
            ))}
          </select>
        </label>
      </div>
      {loaded.status === "loading" ? <Loading what="the portfolio" /> : null}
      {loaded.status === "error" ? <LoadError error={loaded.error} what="portfolio" /> : null}
      {loaded.status === "ready" ? <PortfolioTable data={loaded.data} currency={currency} columns={columns} role={me.role} /> : null}
    </section>
  );
}

function PortfolioTable({ data, currency, columns, role }: { data: PortfolioView; currency: string; columns: Column<PortfolioLine>[]; role: string }) {
  return (
    <>
      <p className="mb-2 text-sm text-ink">
        Total {money(String(data.total_consumption.value), currency)}
        {data.complete ? "" : " (incomplete: some customers' rows have no rate for this currency, and are not in the total)"}
      </p>
      <div className="mb-2">
        <Provenance metric={data.total_consumption} />
      </div>
      <DataTable
        id="portfolio"
        caption={`Portfolio in ${currency}`}
        rows={data.customers}
        rowKey={(l) => l.tenant.id}
        columns={columns}
        role={role}
        empty={{ title: "No customers in scope", next: "Customers appear here once they are assigned to you." }}
      />
    </>
  );
}

export function ReportsList() {
  useAssistantContext({ screen: "reports" });
  const me = useMe();
  const loaded = useApi((t) => apiGet(t, "/v1/reports/packs"), "packs");
  if (loaded.status === "loading") return <Loading what="report packs" />;
  if (loaded.status === "error") return <LoadError error={loaded.error} what="report packs" />;
  const columns: Column<ReportPackSummary>[] = [
    {
      key: "pack",
      header: "Pack",
      value: (p) => `${p.tenant.name} ${p.period} v${p.version}`,
      render: (p) => (
        <Link href={`/reports/${p.id}`} className="link font-medium">
          {p.tenant.name} · {p.period} · version {p.version}
        </Link>
      ),
      sortable: true,
    },
    { key: "status", header: "Status", value: (p) => p.status, render: (p) => <HealthBadge band={STATUS[p.status].band} label={STATUS[p.status].label} />, sortable: true },
    { key: "currency", header: "Contract currency", value: (p) => p.contract_currency, sortable: true },
    { key: "generated", header: "Generated", value: (p) => p.generated_at, render: (p) => date(p.generated_at), sortable: true },
    { key: "published", header: "Published", value: (p) => p.published_at ?? "", render: (p) => date(p.published_at), sortable: true },
  ];
  return (
    <>
      <PageHeader
        title="Reports"
        subtitle={
          me.kind === "customer"
            ? "Your monthly service reports, as published by Getronics."
            : "Monthly packs per customer, generated from live data. Narrative is drafted for a person to edit, and nothing is published without one."
        }
      />
      {can(me, "reports.manage") ? <Generate /> : null}
      <DataTable
        id="report-packs"
        caption="Report packs"
        rows={loaded.data}
        rowKey={(p) => p.id}
        columns={columns}
        facets={[
          { key: "status", label: "Status", value: (p) => STATUS[p.status].label },
          { key: "period", label: "Month", value: (p) => p.period },
        ]}
        role={me.role}
        initial={{ sort: { key: "generated", dir: "desc" }, facets: {} }}
        empty={
          me.kind === "customer"
            ? { title: "No published reports yet", next: "Your service manager publishes each month's report here." }
            : { title: "No packs yet", next: "Generate a month for a customer to start." }
        }
      />
      {me.kind === "staff" && !me.subscription_limited ? <Portfolio /> : null}
    </>
  );
}
