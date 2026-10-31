// Named shapes the console uses, taken from the generated schema: an alias, never
// a hand-written type. Add a line here when a screen needs another one.
import schema from "../openapi.json" with { type: "json" };

import type { components, operations, paths } from "./api";

export type { components, operations, paths };

type Schemas = components["schemas"];

export type Me = Schemas["MeView"];
export type WorkItemList = Schemas["WorkItemList"];
export type WorkItemSummary = Schemas["WorkItemSummary"];
export type WorkItemDetail = Schemas["WorkItemDetail"];
export type TriageView = Schemas["TriageView"];
export type ReviewView = Schemas["ReviewView"];
export type ReviewRequest = Schemas["ReviewRequest"];
export type ResourceContext = Schemas["ResourceContext"];
export type CustomerOverview = Schemas["CustomerOverview"];
export type MetricInt = Schemas["Metric_int_"];
export type MetricDecimal = Schemas["Metric_Decimal_"];
export type ApiErrorBody = { error: { code: string; request_id: string } };
export type TriageOverrides = Schemas["TriageOverrides"];
export type QuarantineList = Schemas["QuarantineList"];
export type QuarantinedAlias = Schemas["QuarantinedAlias"];
export type TenantAliases = Schemas["TenantAliases"];
export type ProposalView = Schemas["ProposalView"];
export type ProposalRequest = Schemas["ProposalRequest"];
export type ReprocessView = Schemas["ReprocessView"];
export type ReprocessPreview = Schemas["ReprocessPreview"];
export type ConnectorHealthList = Schemas["ConnectorHealthList"];
export type ConnectorHealth = Schemas["ConnectorHealth"];
export type DriftList = Schemas["DriftList"];
export type DriftView = Schemas["DriftView"];
export type AcknowledgeRequest = Schemas["AcknowledgeRequest"];
// Step 18: the customer portal's views, the tenant's own users, and onboarding.
export type Performance = Schemas["Performance"];
export type MonthPerformance = Schemas["MonthPerformance"];
export type Posture = Schemas["Posture"];
export type OpenFinding = Schemas["OpenFinding"];
export type Approvals = Schemas["Approvals"];
export type PendingChange = Schemas["PendingChange"];
export type TenantUser = Schemas["TenantUser"];
export type RoleOption = Schemas["RoleOption"];
export type TenantUserRequest = Schemas["TenantUserRequest"];
export type Onboarding = Schemas["Onboarding"];
export type CatalogueRole = Schemas["CatalogueRole"];
export type FederationRequest = Schemas["FederationRequest"];
// Step 16: FinOps, margin and pricing.
export type FinOpsOverview = Schemas["FinOpsOverview"];
export type CustomerCost = Schemas["CustomerCost"];
export type CustomerFinOps = Schemas["CustomerFinOps"];
export type ChargeLine = Schemas["ChargeLine"];
export type AccountCheck = Schemas["AccountCheck"];
export type Unallocated = Schemas["Unallocated"];
export type AllocationLine = Schemas["AllocationLine"];
export type AnomalyView = Schemas["AnomalyView"];
export type FxCorrectionView = Schemas["FxCorrectionView"];
export type FxCorrectionRequest = Schemas["FxCorrectionRequest"];
export type MarginOverview = Schemas["MarginOverview"];
export type MarginView = Schemas["MarginView"];
export type MarginDetail = Schemas["MarginDetail"];
export type TrendPoint = Schemas["TrendPoint"];
export type VarianceView = Schemas["VarianceView"];
export type VersionView = Schemas["VersionView"];
export type RateCardView = Schemas["RateCardView"];
export type QuoteRequest = Schemas["QuoteRequest"];
export type QuoteView = Schemas["QuoteView"];

// The values a reviewer may choose, read from the schema itself rather than
// copied, so a new intent in the API reaches the console by regeneration.

type JsonSchema = { enum?: unknown[]; anyOf?: JsonSchema[]; properties?: Record<string, JsonSchema> };

function enumOf<T extends string>(model: string, field: string): readonly T[] {
  const schemas = (schema as { components: { schemas: Record<string, JsonSchema> } }).components.schemas;
  const property = schemas[model]?.properties?.[field];
  const values = property?.enum ?? property?.anyOf?.find((s) => s.enum)?.enum;
  if (!values || !values.every((v) => typeof v === "string")) {
    throw new Error(`${model}.${field} has no enum in openapi.json`);
  }
  return values as T[];
}

export const TRIAGE_CHOICES = {
  intent: enumOf<NonNullable<TriageOverrides["intent"]>>("TriageOverrides", "intent"),
  action_class: enumOf<NonNullable<TriageOverrides["action_class"]>>("TriageOverrides", "action_class"),
  risk: enumOf<NonNullable<TriageOverrides["risk"]>>("TriageOverrides", "risk"),
} as const;
