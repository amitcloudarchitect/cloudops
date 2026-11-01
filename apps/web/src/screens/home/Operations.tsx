"use client";

// Operations (ADR 0038): work received by kind and priority, how much the classifier
// triaged, auto remediation (0 until the execution plane exists, said so), and SLA
// attainment against ADR 0030's modelled targets.

import type { KindReceived, OperationsSection as Ops, SlaLine } from "@cloudops/contracts";

import { CompareTable, metricText, type CompareRow } from "@/components/CompareTable";
import { MetricCard } from "@/components/MetricCard";
import { ModelledMark, Provenance } from "@/components/Provenance";
import { apiGet } from "@/lib/api";
import { rangeLabel } from "@/lib/range";
import { useApi } from "@/lib/useApi";

import { Both, HomeSection, Part, queryKey, RangeControls, useRangeChoice, useRangeQuery } from "./frame";

const PATH = "/v1/home/sections/operations";

export const KIND_LABEL: Record<string, string> = {
  incident: "Incidents",
  service_request: "Requests",
  change_task: "Changes",
  project_task: "Tasks",
};
const PRIORITY_LABEL: Record<string, string> = { "1": "P1", "2": "P2", "3": "P3", "4": "P4", "5": "P5", none: "No priority" };

function kindLabel(kind: string): string {
  return KIND_LABEL[kind] ?? kind.replace(/_/g, " ");
}

function byKind(ops: Ops | null, kind: string): KindReceived | undefined {
  return ops?.received.find((k) => k.kind === kind);
}

function slaOf(ops: Ops | null, priority: string): SlaLine | undefined {
  return ops?.sla.find((s) => s.priority === priority);
}

function PriorityMatrix({ ops, caption }: { ops: Ops; caption: string }) {
  const priorities = ["1", "2", "3", "4", "5", "none"];
  return (
    <table className="w-full border-collapse text-sm">
      <caption className="mb-1 text-left text-xs font-semibold text-muted">{caption}</caption>
      <thead>
        <tr className="text-left text-xs text-muted">
          <th scope="col" className="pb-1 pr-2 font-semibold">
            Kind
          </th>
          {priorities.map((p) => (
            <th key={p} scope="col" className="pb-1 pl-1 text-right font-semibold">
              {p === "none" ? <abbr title="No priority">None</abbr> : PRIORITY_LABEL[p]}
            </th>
          ))}
          <th scope="col" className="pb-1 pl-1 text-right font-semibold">
            Total
          </th>
        </tr>
      </thead>
      <tbody>
        {ops.received.map((k) => (
          <tr key={k.kind} className="border-t border-line-subtle">
            <th scope="row" className="py-1.5 pr-2 text-left font-normal text-ink">
              {kindLabel(k.kind)}
            </th>
            {priorities.map((p) => {
              const cell = k.by_priority.find((x) => x.priority === p);
              return (
                <td key={p} className="py-1.5 pl-1 text-right tabular-nums text-ink">
                  {cell ? metricText(cell.received) : "—"}
                </td>
              );
            })}
            <td className="py-1.5 pl-1 text-right font-semibold tabular-nums text-ink">{metricText(k.received)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function OperationsSection() {
  const [choice, setChoice] = useRangeChoice();
  const qa = useRangeQuery(choice.a);
  const qb = useRangeQuery(choice.b);
  const a = useApi((t) => (qa ? apiGet(t, PATH, {}, qa) : Promise.reject(new Error("invalid range"))), queryKey(PATH, qa));
  const b = useApi((t) => (qb ? apiGet(t, PATH, {}, qb) : Promise.resolve(null)), queryKey(PATH, qb));
  return (
    <HomeSection
      id="operations"
      title="Operations"
      subtitle="Work received, triaged and remediated, and SLA attainment, for the range you choose."
      href="/work"
    >
      <RangeControls section="Operations" choice={choice} onChange={setChoice} />
      <Both a={a} b={b} what="operations">
        {(ops, other) => {
          const headings = other ? { a: rangeLabel(choice.a), b: choice.b ? rangeLabel(choice.b) : "" } : { a: rangeLabel(choice.a) };
          const kinds = [...new Set([...ops.received.map((k) => k.kind), ...(other?.received.map((k) => k.kind) ?? [])])];
          const received: CompareRow[] = [
            ...kinds.map((k) => ({ key: k, label: kindLabel(k), a: byKind(ops, k)?.received, b: byKind(other, k)?.received })),
            { key: "total", label: "All work received", a: ops.received_total, b: other?.received_total, total: true },
          ];
          const triage: CompareRow[] = ops.triage
            ? [
                { key: "eligible", label: "Incidents and requests received", a: ops.triage.eligible, b: other?.triage?.eligible },
                { key: "triaged", label: "Auto-triaged", a: ops.triage.triaged, b: other?.triage?.triaged },
                { key: "ready", label: "Routed on to planning", a: ops.triage.ready_for_planning, b: other?.triage?.ready_for_planning },
                { key: "human", label: "Routed to a person", a: ops.triage.human, b: other?.triage?.human },
                { key: "share", label: "Share auto-triaged", a: ops.triage.share, b: other?.triage?.share, missing: "Nothing received" },
              ]
            : [];
          const sla: CompareRow[] = [
            ...["1", "2", "3", "4", "5", "none"].map((p) => {
              const line = slaOf(ops, p);
              const target = line?.target_hours ? ` (target ${metricText(line.target_hours)})` : "";
              return {
                key: p,
                label: `${PRIORITY_LABEL[p]}${target}`,
                a: line?.attainment,
                b: slaOf(other, p)?.attainment,
                missing: line?.target_hours ? "None resolved" : "No target",
              };
            }),
            { key: "all", label: "All priorities with a target", a: ops.attainment, b: other?.attainment, missing: "None resolved", total: true },
          ];
          return (
            <div className="flex flex-col gap-5">
              <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
                <MetricCard label="Work received" metric={ops.received_total} display={metricText} href="/work" />
                {ops.triage ? (
                  <MetricCard label="Auto-triaged" metric={ops.triage.triaged} display={metricText} href="/observability" />
                ) : null}
                <MetricCard label="Auto-remediated" metric={ops.remediation.auto_remediated} display={metricText} />
                <MetricCard label="SLA attainment" metric={ops.attainment} display={metricText} href="/performance" unavailable="No incident with a target was resolved in the range." />
              </div>
              <p className="rounded-control border border-dashed border-line-subtle px-3 py-2 text-sm text-muted" data-testid="remediation-note">
                <strong className="font-semibold text-ink">Auto-remediation: not yet available.</strong> The execution plane that runs signed runbooks is not built
                yet, so nothing is remediated automatically and the figure is 0, not unknown.
              </p>
              <div className="grid gap-5 xl:grid-cols-2">
                <Part title="Received by kind">
                  <CompareTable caption="Work received by kind" component="Kind" {...headings} rows={received} provenance={ops.received_total} />
                </Part>
                {ops.triage ? (
                  <Part title="Auto-triage">
                    <CompareTable caption="Incidents and requests the classifier triaged, by route" {...headings} rows={triage} provenance={ops.triage.triaged} />
                  </Part>
                ) : null}
              </div>
              <Part title="Received by priority">
                <div className="grid gap-4 xl:grid-cols-2">
                  <PriorityMatrix ops={ops} caption={`${rangeLabel(choice.a)}: work received by kind and priority`} />
                  {other && choice.b ? <PriorityMatrix ops={other} caption={`${rangeLabel(choice.b)}: work received by kind and priority`} /> : null}
                </div>
              </Part>
              <Part title="SLA attainment">
                <CompareTable caption="Incidents resolved within their priority's modelled target, by priority" component="Priority" {...headings} rows={sla} />
                <p className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted">
                  <ModelledMark />
                  Attainment is modelled: an incident resolved in the range counts as met when its resolve time is within its priority&rsquo;s target (P1 4 h, P2 8 h, P3 72 h,
                  P4 120 h; platform policy, ADR 0030). P5 has no target. ServiceNow&rsquo;s own breach flag is kept apart.
                </p>
                <div className="mt-1">
                  <Provenance metric={ops.sla[0]?.resolved ?? ops.received_total} />
                </div>
              </Part>
            </div>
          );
        }}
      </Both>
    </HomeSection>
  );
}
