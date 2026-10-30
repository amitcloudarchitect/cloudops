# ADR 0012: Azure OpenAI for dev — region, residency, models and quota

- **Status:** Accepted, 2026-10-30
- **Decided by:** Amit Kumar
- **Implements:** LLD "AI platform configuration" and open item L12 (model per tier).
  Stack `infra/terraform/stacks/03a-ai`; the role in `04b-rbac`.

## Decisions

1. **One Azure OpenAI resource in West Europe**, beside the rest of dev
   (`oai-cloudops-dev-weu`, resource group `rg-cloudops-ai-dev-weu`). Sweden Central
   has more models and capacity, and was not chosen.
2. **EU Data Zone deployments (`DataZoneStandard`).**
   - Customer ticket text is processed only in EU Azure regions.
   - West Europe offers almost nothing as a regional deployment: only `gpt-4.1-mini`,
     `text-embedding-ada-002` and `whisper` on 2026-10-30. A regional-only dev would
     have had no large tier.
   - Global deployments were rejected: data may be processed outside the EU.
3. **Models per tier**, pinned with automatic version upgrades off:

   | Tier | Deployment | Model | Version | Retires | Dev capacity (K TPM) |
   |---|---|---|---|---|---|
   | small | `cloudops-small` | `gpt-5.4-mini` | 2026-03-17 | 2027-09 | 50 |
   | large | `cloudops-large` | `gpt-5.5` | 2026-04-24 | 2027-10 | 30 |
   | embed | `cloudops-embed` | `text-embedding-3-small` | 1 | 2028-02 | 50 |

   - The embedding model's 1536 dimensions match the database's `vector(1536)`
     columns, so no migration.
   - Capacity is well inside the subscription's Data Zone quota in West Europe (1,000K,
     3,000K and 10,000K respectively). It is a variable: other environments size it.
4. **Private endpoint and Entra ID only, as the LLD says.** Public network access is
   off, and local (key) authentication is off, so no API key exists. The AI workload
   identity holds Cognitive Services OpenAI User on the account (04b): it can call the
   deployments, not change them.
5. **The stack is `03a-ai`**: after network, security and observability (subnet, DNS
   zone, Log Analytics), and before `04b-rbac`, which grants access to it. Stacks
   apply in name order.
6. **Diagnostics:** audit and request logs to Log Analytics. These are operation
   metadata (caller, deployment, tokens), not prompt content.
7. **The gateway calls it as the workload identity** (`cloudops_gateway.azure_openai`).
   - Entra token for `https://cognitiveservices.azure.com/.default`, from
     azure-identity in the worker; the gateway itself has no Azure dependency.
   - API version `2024-10-21` (GA). These models refuse `max_tokens`; the provider
     sends `max_completion_tokens`.
   - 429 is throttled; 5xx, timeouts, connection failures and a missing deployment
     are unavailable; both fall back (here only `large` to `small`). Anything else,
     such as a content-filter refusal, is a provider error. Errors carry the HTTP
     status and Azure's error code, never prompt or completion text.
   - Embeddings of any size other than 1536 are refused.
   - Contract tests replay responses recorded on 2026-10-30 from the dev account,
     through the private endpoint, as the AI identity.
8. **Prices are Microsoft's list prices** (Retail Prices API, West Europe, Data Zone
   Standard, read 2026-10-30), recorded in USD because the API rounds EUR to too few
   decimals (the embedding price reads 0.00 in EUR). Per 1,000 tokens:

   | Deployment | Input | Output | Meters |
   |---|---|---|---|
   | `cloudops-small` | $0.000825 | $0.00495 | e378a12b, 69008474 |
   | `cloudops-large` | $0.0055 | $0.033 | db528725, f7f9d475 |
   | `cloudops-embed` | $0.000022 | — | 08d886b7 |

   `gpt-5.5` has a long-context band (input $0.011, output $0.0495) above its context
   threshold; the gateway's prompts stay far below it. **Later:** Getronics' contract
   pricing (CSP or another channel) replaces list price.
9. **No secondary provider for now** (decided): each tier has one deployment, and the
   only fallback is `large` to `small`. No second region.

## Open

- **Abuse-monitoring opt-out:** not needed in dev (decided). Apply to Microsoft before
  UAT, when real customer data first reaches the model. Until approved, Microsoft
  may keep prompts for up to 30 days for abuse review.
- **Secondary provider** for throttling (LLD): deferred (decision 9).
- **An end-to-end triage in dev** needs a ticket with a tenant: every real ServiceNow
  ticket is quarantined until the customer-mapping screen exists.
- **Retirement:** both chat models retire in 2027. Replacing one goes through the
  evaluation gate, not a version bump.
