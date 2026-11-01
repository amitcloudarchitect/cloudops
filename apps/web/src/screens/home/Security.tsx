"use client";

// Security & compliance (ADR 0038): vulnerabilities open at the end of the range by
// severity and age, found and fixed in it; for staff, assessment scores (ADR 0020)
// and CoreStack's security events. The ages are the posture view's bands (ADR 0036).

import type { SecuritySection as Security } from "@cloudops/contracts";

import { CompareTable, metricText, type CompareRow } from "@/components/CompareTable";
import { HealthBadge } from "@/components/HealthBadge";
import { MetricCard } from "@/components/MetricCard";
import { Provenance } from "@/components/Provenance";
import { Gap } from "@/components/Widget";
import { apiGet } from "@/lib/api";
import { date } from "@/lib/format";
import { band, THRESHOLDS } from "@/lib/health";
import { rangeLabel } from "@/lib/range";
import { useApi } from "@/lib/useApi";

import { Both, HomeSection, Part, queryKey, RangeControls, useRangeChoice, useRangeQuery } from "./frame";

const PATH = "/v1/home/sections/security";
const SEVERITY: Record<number, string> = { 5: "Severity 5 (urgent)", 4: "Severity 4 (critical)", 3: "Severity 3 (serious)", 2: "Severity 2 (medium)", 1: "Severity 1 (minimal)" };
const EVENT: Record<string, string> = { high: "High", medium: "Medium", low: "Low" };

function AgeTable({ s, caption }: { s: Security; caption: string }) {
  const bands = s.vulnerabilities[0]?.by_age.map((a) => a.band) ?? [];
  return (
    <table className="w-full border-collapse text-sm">
      <caption className="mb-1 text-left text-xs font-semibold text-muted">{caption}</caption>
      <thead>
        <tr className="text-left text-xs text-muted">
          <th scope="col" className="pb-1 pr-2 font-semibold">
            Severity
          </th>
          {bands.map((b) => (
            <th key={b} scope="col" className="pb-1 pl-1 text-right font-semibold">
              {b} days
            </th>
          ))}
          <th scope="col" className="pb-1 pl-1 text-right font-semibold">
            Open
          </th>
          <th scope="col" className="pb-1 pl-1 text-right font-semibold">
            Found
          </th>
          <th scope="col" className="pb-1 pl-1 text-right font-semibold">
            Fixed
          </th>
        </tr>
      </thead>
      <tbody>
        {s.vulnerabilities.map((v) => (
          <tr key={v.severity} className="border-t border-line-subtle">
            <th scope="row" className="py-1.5 pr-2 text-left font-normal text-ink">
              {SEVERITY[v.severity] ?? v.severity}
            </th>
            {v.by_age.map((a) => (
              <td key={a.band} className="py-1.5 pl-1 text-right tabular-nums text-ink">
                {metricText(a.open)}
              </td>
            ))}
            <td className="py-1.5 pl-1 text-right font-semibold tabular-nums text-ink">{metricText(v.open)}</td>
            <td className="py-1.5 pl-1 text-right tabular-nums text-ink">{metricText(v.found)}</td>
            <td className="py-1.5 pl-1 text-right tabular-nums text-ink">{metricText(v.fixed)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function SecuritySection({ customer }: { customer: boolean }) {
  const [choice, setChoice] = useRangeChoice();
  const qa = useRangeQuery(choice.a);
  const qb = useRangeQuery(choice.b);
  const a = useApi((t) => (qa ? apiGet(t, PATH, {}, qa) : Promise.reject(new Error("invalid range"))), queryKey(PATH, qa));
  const b = useApi((t) => (qb ? apiGet(t, PATH, {}, qb) : Promise.resolve(null)), queryKey(PATH, qb));
  return (
    <HomeSection
      id="security"
      title="Security & compliance"
      subtitle="Vulnerabilities open at the end of the range by severity and age, what was found and fixed in it, compliance scores and security events."
      href={customer ? "/posture" : "/secops"}
    >
      <RangeControls section="Security and compliance" choice={choice} onChange={setChoice} />
      <Both a={a} b={b} what="security and compliance">
        {(s, other) => {
          const headings = other && choice.b ? { a: rangeLabel(choice.a), b: rangeLabel(choice.b) } : { a: rangeLabel(choice.a) };
          const major = s.vulnerabilities.filter((v) => v.severity >= 4).reduce((n, v) => n + Number(v.open.value), 0);
          const open: CompareRow[] = [
            ...s.vulnerabilities.map((v) => ({
              key: String(v.severity),
              label: SEVERITY[v.severity] ?? String(v.severity),
              a: v.open,
              b: other?.vulnerabilities.find((x) => x.severity === v.severity)?.open,
            })),
            { key: "total", label: "All open", a: s.open_total, b: other?.open_total, total: true },
          ];
          const events: CompareRow[] = s.events
            ? [
                ...s.events.by_severity.map((e) => ({
                  key: e.severity,
                  label: `${EVENT[e.severity] ?? e.severity}: detected in the range`,
                  a: e.detected,
                  b: other?.events?.by_severity.find((x) => x.severity === e.severity)?.detected,
                })),
                ...s.events.by_severity.map((e) => ({
                  key: `${e.severity}-open`,
                  label: `${EVENT[e.severity] ?? e.severity}: open at the end`,
                  a: e.open,
                  b: other?.events?.by_severity.find((x) => x.severity === e.severity)?.open,
                })),
                { key: "compliance", label: "Compliance findings open at the end", a: s.events.compliance_open, b: other?.events?.compliance_open },
              ]
            : [];
          return (
            <div className="flex flex-col gap-5">
              <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
                <MetricCard label="Open vulnerabilities" metric={s.open_total} display={metricText} href={customer ? "/posture" : "/secops"} />
                <MetricCard
                  label="Open at severity 4 or 5"
                  metric={s.vulnerabilities.find((v) => v.severity === 5)?.open ?? null}
                  display={() => String(major)}
                  band={band(major, THRESHOLDS.criticalVulnerabilities)}
                  href={customer ? "/posture" : "/secops"}
                />
                {s.assessments !== null ? (
                  <MetricCard label="Average assessment score" metric={s.assessment_average} display={metricText} href="/assurance" unavailable="No assessment has an approved response yet." />
                ) : null}
              </div>
              <div className="grid gap-5 xl:grid-cols-2">
                <Part title="Open vulnerabilities by severity">
                  <CompareTable caption="Vulnerabilities open at the end of each range, by severity" component="Severity" {...headings} rows={open} provenance={s.open_total} />
                </Part>
                <Part title="By age at the end of the range">
                  <AgeTable s={s} caption={`${rangeLabel(choice.a)}: open by severity and age, found and fixed`} />
                  {other && choice.b ? (
                    <div className="mt-3">
                      <AgeTable s={other} caption={`${rangeLabel(choice.b)}: open by severity and age, found and fixed`} />
                    </div>
                  ) : null}
                </Part>
              </div>
              {s.events ? (
                <Part title="Security events">
                  <CompareTable caption="CoreStack security events by severity, and compliance findings" component="Events" {...headings} rows={events} provenance={s.events.compliance_open} />
                </Part>
              ) : customer ? null : (
                <Gap text="Security events: not available to your role." />
              )}
              {s.assessments !== null ? (
                <Part title="Compliance assessments">
                  {s.assessments.length > 0 ? (
                    <table className="w-full border-collapse text-sm">
                      <caption className="sr-only">The latest assessment of each customer and its score</caption>
                      <thead>
                        <tr className="text-left text-xs text-muted">
                          <th scope="col" className="pb-1 font-semibold">Customer</th>
                          <th scope="col" className="pb-1 font-semibold">Assessment</th>
                          <th scope="col" className="pb-1 font-semibold">Opened</th>
                          <th scope="col" className="pb-1 text-right font-semibold">Score</th>
                        </tr>
                      </thead>
                      <tbody>
                        {s.assessments.map((x) => (
                          <tr key={`${x.tenant.id}-${x.title}`} className="border-t border-line-subtle">
                            <th scope="row" className="py-1.5 pr-2 text-left font-normal text-ink">{x.tenant.name}</th>
                            <td className="py-1.5 pr-2 text-ink">
                              {x.framework} · {x.title}
                            </td>
                            <td className="py-1.5 pr-2 text-ink">{date(x.opened_at)}</td>
                            <td className="py-1.5 text-right tabular-nums text-ink">
                              {x.score ? metricText(x.score) : <HealthBadge band="unknown" label="Not scored yet" />}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  ) : (
                    <Gap text="No assessment opened by the end of the range." />
                  )}
                  {s.assessment_average ? (
                    <div className="mt-2">
                      <Provenance metric={s.assessment_average} />
                    </div>
                  ) : null}
                </Part>
              ) : null}
            </div>
          );
        }}
      </Both>
    </HomeSection>
  );
}
