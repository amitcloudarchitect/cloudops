# ADR 0005: The AI gateway, tool registry and retrieval

- **Status:** Accepted, 2026-10-28
- **Decided by:** Amit Kumar (step 06 carried out on this design)
- **Implements:** LLD, AI platform configuration (Gateway controls, Vector store);
  `services/gateway`; migration `0006_ai_gateway`

## Decisions the LLD leaves open

1. **Gateway state lives in PostgreSQL, under the same RLS as everything else.**
   - The usage ledger and the trace are one table, `cloudops.llm_call`: one row per
     call, holding the redacted prompt, retrieval set, tool calls, tokens, latency,
     and cost as `numeric(18,6)` with a currency.
   - The semantic cache is `cloudops.llm_cache`.
   - Both carry `tenant_id NOT NULL` with `FORCE`d isolation.
   - **Why not Redis:** a semantic cache keyed only on prompt similarity can serve
     one customer's answer to another, and Redis has no row-level security. Keeping
     the cache in the database means the one isolation rule, and the one suite that
     tests it, covers it (the HLD's reason for choosing pgvector). Redis can front
     the quota counters later if latency demands it; the ledger remains the record.
2. **Cache entries are also keyed by role class and subscription scope.**
   - Without this, a CFO's cached answer could reach an engineer in the same
     tenant, and a full-tenant answer could reach a subscription-limited user.
   - TTLs follow the LLD: 1 hour for live state, 24 hours for static knowledge.
   - Caching happens only when the caller asks for it, and never for tool-using
     turns.
3. **One tenant per call.** `CallContext.for_tenant` narrows the caller's resolved
   scope (ADR 0002) to the conversation's tenant, after checking that tenant is in
   that scope. Tools and retrieval run under the narrowed scope, and nothing a model
   emits can change it.
4. **Tools declare a matrix permission, not a role.** The LLD says each tool
   "declares its required role". Under ADR 0002 (D8, D13) roles are data, and
   permissions are what the matrix grants, so a tool names a permission
   (`inventory.read`). That is the same check the API's BFF makes.
5. **No model can be given a way to name scope.**
   - At registration, a schema is refused if it contains `tenant_id`, `customer`,
     `role`, `principal`, `account_ids` or similar, or if it does not close itself
     with `additionalProperties: false`.
   - At call time, an argument outside the schema is a refusal.
   - Write tools are refused until the approval machine exists, and must declare an
     idempotency key to register at all.
6. **Spend breaker.** Spend over the last 15 minutes is compared with the trailing
   24-hour rate, both per tenant and platform-wide; above five times, once past a
   floor, the circuit opens.
   - The platform-wide view needs spend across tenants. It reads it through
     `cloudops.llm_platform_spend`, a `SECURITY DEFINER` function owned by
     `cloudops_resolver` that returns two totals. This is the same narrow pattern as
     ADRs 0002 and 0004.
   - The catalog test now allows three resolver policies, each confined to that
     role.
7. **Redaction covers every model call, embeddings included**, and everything that
   enters a prompt: user text, retrieved chunks and tool results.
   - Secrets are redacted by pattern, as are email, phone, card (Luhn-checked), IBAN
     (mod-97) and UK National Insurance numbers.
   - People's names are redacted only when the caller supplies them as known
     values, because no pattern finds names reliably.
   - IP addresses, host names and ticket numbers are kept: they are operational data
     the model needs.

## Consequences

- **Quotas** are checked against usage before a call, so one call may overshoot and
  the next is refused. From 80% the reply carries a warning flag.
- **Fallback:** throttling or an outage moves to the next deployment, and `large`
  falls back to `small`. Every hop is traced.
- **The tool loop** stops after 10 iterations and records an `escalated` call for a
  person. The model never chooses what happens next.
- **Retrieval** is one SQL statement:
  - four ranked branches, vector (HNSW) and full-text, over the tenant's chunks and
    over Getronics-wide chunks;
  - fused by reciprocal rank (k = 60);
  - with the tenant predicate inside every customer branch.

  A test (isolation case 3) makes another tenant's chunk the perfect match for an
  adversarial query, and proves it never appears.
- **Not built:**
  - **The Azure OpenAI provider:** LLD L12 (the model per tier) is open, and the
    provider needs the private endpoint and managed identity.
  - **Re-ranking by the `small` tier:** to be measured against the evaluation set
    (step 08) before it is switched on.
  - **Rolling context summarisation:** it belongs with the agents (step 07).
- **Prices** in `pricing.py` are local placeholders. Real prices arrive with L12, in
  the same file, which is the gateway's rate card.
- **`llm_call` is not partitioned.** The LLD partitions `message` monthly;
  `llm_call` will need the same once volume is known.
