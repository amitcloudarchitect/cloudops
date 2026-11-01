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
export type ResourceList = Schemas["ResourceList"];
export type ResourceStaffView = Schemas["ResourceStaffView"];
export type CustomerOverview = Schemas["CustomerOverview"];
export type CustomerPrice = Schemas["CustomerPrice"];
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
export type CloudCredential = Schemas["CredentialView"];
export type CloudCheck = Schemas["CheckView"];
export type AzureCredentialRequest = Schemas["AzureCredentialRequest"];
export type AwsCredentialRequest = Schemas["AwsCredentialRequest"];
export type CredentialCheckRequest = Schemas["CheckRequest"];
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
// Step 17: knowledge, assurance and reporting.
export type DocumentList = Schemas["DocumentList"];
export type DocumentView = Schemas["DocumentView"];
export type CoverageView = Schemas["CoverageView"];
export type CitationView = Schemas["CitationView"];
export type FrameworkView = Schemas["FrameworkView"];
export type ControlView = Schemas["ControlView"];
export type AssessmentSummary = Schemas["AssessmentSummary"];
export type AssessmentDetail = Schemas["AssessmentDetail"];
export type ResponseView = Schemas["ResponseView"];
export type EvidenceView = Schemas["EvidenceView"];
export type ScoreView = Schemas["ScoreView"];
export type Approval = Schemas["Approval"];
export type EvidencePackSummary = Schemas["EvidencePackSummary"];
export type ReportPackSummary = Schemas["ReportPackSummary"];
export type ReportPackView = Schemas["ReportPackView"];
export type NarrativeView = Schemas["NarrativeView"];
export type SectionSnapshot = Schemas["SectionSnapshot"];
export type FigureSnapshot = Schemas["FigureSnapshot"];
export type TableSnapshot = Schemas["TableSnapshot"];
export type PortfolioView = Schemas["PortfolioView"];
export type PortfolioLine = Schemas["PortfolioLine"];
// The AMARA assistant (ADR 0029).
export type AssistantSession = Schemas["AssistantSession"];
export type TenantOption = Schemas["TenantOption"];
export type TurnView = Schemas["TurnView"];
export type AnswerView = Schemas["AnswerView"];
export type SourceView = Schemas["SourceView"];
export type ClaimView = Schemas["ClaimView"];
export type ConversationSummary = Schemas["ConversationSummary"];
export type ConversationView = Schemas["ConversationView"];
export type AskRequest = Schemas["AskRequest"];
export type ScreenContext = Schemas["ScreenContext"];
export type EnabledRequest = Schemas["EnabledRequest"];
export type EnabledView = Schemas["EnabledView"];

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
// The home dashboards (ADR 0030).
export type OperationsHome = Schemas["OperationsHome"];
export type PriorityLine = Schemas["PriorityLine"];
export type ClosestItem = Schemas["ClosestItem"];
export type TenantBacklog = Schemas["TenantBacklog"];
export type WorkgroupBacklog = Schemas["WorkgroupBacklog"];
export type TriageHome = Schemas["TriageHome"];
export type CostHome = Schemas["CostHome"];
export type CustomerCostHome = Schemas["CustomerCostHome"];
export type AiUsageHome = Schemas["AiUsageHome"];
export type PriceSummary = Schemas["PriceSummary"];
// Platform observability (migration 0032).
export type UsageView = Schemas["UsageView"];
export type UsageDay = Schemas["UsageDay"];
export type UsageWeek = Schemas["UsageWeek"];
export type RoleUsers = Schemas["RoleUsers"];
export type EndpointUse = Schemas["EndpointUse"];
export type AiView = Schemas["AiView"];
export type AiDay = Schemas["AiDay"];
export type AiAgent = Schemas["AiAgent"];
export type AiCustomer = Schemas["AiCustomer"];
export type AiFigures = Schemas["AiFigures"];
export type TriageThroughput = Schemas["TriageThroughput"];
export type TriageDay = Schemas["TriageDay"];
export type TriageCustomer = Schemas["TriageCustomer"];
export type ConnectorSummary = Schemas["ConnectorSummary"];
export type SourceHealth = Schemas["SourceHealth"];
export type PlatformCost = Schemas["PlatformCost"];
// Settings → Commercial setup (ADR 0033, ADR 0035).
export type RateCardVersionView = Schemas["RateCardVersionView"];
export type RateCardDocument = Schemas["RateCardDocument"];
export type PublishRateCard = Schemas["PublishRateCard"];
export type SowRegister = Schemas["SowRegister"];
export type SowVersionView = Schemas["SowVersionView"];
export type SowInput = Schemas["SowInput"];
export type RecomputeView = Schemas["RecomputeView"];
// The SecOps workspace (ADR 0036).
export type SecOpsVulnerabilities = Schemas["SecOpsVulnerabilities"];
export type SecOpsFinding = Schemas["SecOpsFinding"];
export type SeverityAgeing = Schemas["SeverityAgeing"];
export type CustomerAgeing = Schemas["CustomerAgeing"];
export type SecOpsCoverage = Schemas["SecOpsCoverage"];
export type CustomerCoverage = Schemas["CustomerCoverage"];
export type UnmatchedResource = Schemas["UnmatchedResource"];
export type QuarantinedHost = Schemas["QuarantinedHost"];
export type SecOpsPostureScores = Schemas["SecOpsPostureScores"];
export type CustomerPostureScore = Schemas["CustomerPostureScore"];
export type FindingDetail = Schemas["FindingDetail"];
export type SlaBand = SecOpsFinding["sla"];
// The home sections and the size band settings (ADR 0038).
export type OperationsSection = Schemas["OperationsSection"];
export type KindReceived = Schemas["KindReceived"];
export type SlaLine = Schemas["SlaLine"];
export type RevenueSection = Schemas["RevenueSection"];
export type HostingLine = Schemas["HostingLine"];
export type ManagedLine = Schemas["ManagedLine"];
export type PriceLine = Schemas["PriceLine"];
export type SecuritySection = Schemas["SecuritySection"];
export type SeverityLine = Schemas["SeverityLine"];
export type PlatformSection = Schemas["PlatformSection"];
export type CustomerSize = Schemas["CustomerSize"];
export type TopCustomers = Schemas["TopCustomers"];
export type TopCustomer = Schemas["TopCustomer"];
export type HealthPart = Schemas["HealthPart"];
export type SizeBandSettings = Schemas["SizeBandSettings"];
export type SizeBandsInput = Schemas["SizeBandsInput"];
