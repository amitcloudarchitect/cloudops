# ADR 0029: The AMARA assistant

- **Status:** Accepted, 2026-11-01
- **Decided by:** Amit Kumar (the questions below); the rest is the HLD's "How a
  question is answered" carried out on this design
- **Implements:** HLD "AI platform and the assistant"; LLD Gateway controls, Vector
  store, Evaluation and isolation case 4. Migration `0023_assistant`,
  `cloudops_agents.assistant` (with prompts `assistant_classifier`, `assistant_reranker`,
  `assistant_answerer`, `assistant_help`), `cloudops_agents.help_corpus` and `help/`,
  `cloudops_api.assistant`, new read-only tools in `cloudops_gateway.tools`, the
  console's AMARA panel, `evals/assistant`, stack `04b-rbac` (`api_openai_user`).
- **Amends:** ADR 0001 (the global knowledge table gains a row-level policy), ADR 0005
  (quotas read through `cloudops.llm_usage`; ledger ids from the sequence), ADR 0018
  (a customer scope may write, never read, the gateway's ledger and trace).

## Context

The HLD names an assistant ("AMARA") in the presentation layer and says how a
question is answered, but no build step covers it. The user asked for a chat panel on
every screen of the console and the portal. It takes the context of the screen and
record the user is on, offers suggested questions, and answers questions about the
account and about how the platform works. The HLD and LLD leave open who gets it,
history, streaming, comparisons, where it runs, and how a customer scope (which has no
grant on the gateway's tables, ADR 0018) records its calls. These were asked and
decided as below.

## Decisions

| # | Question | Decision |
| --- | --- | --- |
| D1 | Audience | Staff and customers in the same release. A customer's assistant is off until a platform administrator switches it on for the tenant (`tenant.assistant_enabled`), after the customer-role evaluation cases pass. |
| D2 | History | Kept 90 days after the conversation's last turn (`cloudops.assistant_purge`, daily from the AI worker). Users list, reopen and delete their own conversations only. |
| D3 | Streaming | No token streaming. Whole, schema-validated answers whose citations are checked before display; the panel polls the turn and shows its stage. |
| D4 | Comparisons | Refused for everyone. A conversation is bound to exactly one tenant (ADR 0005 D3). |
| D5 | Customer trace | `cloudops_customer` may INSERT into `llm_call` and `agent_run`, and nothing else. Quotas and the tenant breaker read through one definer function returning totals and a verdict. |
| D6 | Customer cache | `cloudops_customer` may read and write `llm_cache` under its own scope key only (`cache_scope`). Only tool-free help answers are cached (24 h). |
| D7 | Budget | The assistant (`assistant.*` tasks) has its own sub-budget: at most 40% of the tenant's monthly tokens and 100,000 tokens per user per day, inside the gateway's quotas and breaker. |
| D8 | Staff with no customer chosen | Help-only: platform help, no tools, stored and billed under the internal (Getronics) tenant. |
| D9 | Help corpus | User-facing pages in `cloudops_agents/help`, loaded as global knowledge by the owner-run migrate step. The AI worker fills their embeddings through `cloudops.fill_global_embedding`. |
| D10 | Where a turn runs | In the API process, as a background task. The API's workload identity gets Cognitive Services OpenAI User (stack 04b-rbac, applied by the user). |

### How a turn runs (`cloudops_agents.assistant`)

Code decides every step; the models fill in content at three of them (CLAUDE.md rule 3).

1. **Deterministic guards.** A question that names another customer the caller can see,
   or asks to compare, rank or benchmark customers, is refused before any model sees it.
2. **Intent**, small tier, no tools: account, platform help, comparison, action or other.
   Comparison and action are refused, and "other" is declined. A help-only
   conversation asking about an account is told to choose a customer.
3. **Retrieval.** Hybrid search: pgvector and full-text, fused by reciprocal rank, with
   the tenant predicate inside the SQL. Then the top 12 are re-ranked by the small
   tier, and 5 are kept. Help pages are always searched. Customer documents and close
   notes are searched only for staff holding `knowledge.read`. A search with no tenant
   kinds runs a global-only statement and never touches `knowledge_chunk`.
4. **Screen context.** The client's screen, ticket number or resource id is a hint. It
   is shape-checked by the API, then looked up through the same read-only tools under
   the conversation's scope. Another tenant's record is "not found" and is dropped.
5. **Answer.** Platform help: small tier, no tools, cacheable. Account questions: large
   tier, with the read-only allowlist (`get_ticket`, `search_tickets`,
   `get_sla_summary`, `get_resource_context`, `get_cost_trend`,
   `get_open_vulnerabilities`, `get_recent_changes`, `get_margin`), cut further by the
   registry to the caller's permissions. A customer is never offered `get_margin`, and
   `commercial_visibility` holds even if it were. Ten tool rounds, then "I don't know".
6. **Deterministic check.** Every claim must cite a ref it was given or a tool
   returned, and every number in the answer must appear in that evidence (as given,
   rounded, or a fraction as a percentage). Otherwise the answer is replaced by
   "I don't know". The answer carries its sources, each with its as-of time; modelled
   figures (a margin band on modelled hours) are marked.

The prompts use the delimited data sections (`prompts.build_messages`). Ticket text,
documents, tags and conversation history are data. The context budget is 6,000
tokens of data sections: the lowest-ranked passages are dropped first. History is
the last four answered turns, truncated.

### Scope and isolation

- The conversation's tenant is chosen once, server-side. For a customer it is its
  tenant. For staff it is the screen's customer, if that customer is in their
  resolved scope, and a 404 otherwise; with no customer, it is the internal tenant
  (D8). Nothing a model emits can change it.
- `assistant_conversation` and `assistant_turn` carry `tenant_id` with FORCEd
  `tenant_isolation`. A restrictive `owner_only` policy compares the row's principal
  with `app.principal_id`, which packages/tenancy now sets with `SET LOCAL` and
  verifies at commit, like the tenant ids. History is the author's own by the
  database, not by a `WHERE`.
- **Staff help-only reads** add the internal tenant to the caller's scope for the
  assistant's own tables only, where `owner_only` still applies. The help-only turn
  calls no tool, and its retrieval is the global-only statement.
- `global_knowledge_chunk` now has row-level security for one purpose: a customer
  scope reads `platform_help`, and never `platform_help_staff` or shared runbooks.
- Deleting a conversation also clears the prompt text and agent output recorded for
  it (`cloudops.assistant_forget`), for the same principal and tenant only. The
  purge does the same for anything older than 90 days.
- The isolation suite's case 4 is now real: a customer's comparison is refused before
  any model call, and a tool asked for another tenant's ticket finds nothing.

### Help corpus

The pages were written for end users: what each screen shows, metrics and health
bands, roles, onboarding, glossary. Pages marked `audience: staff` (margin, pricing,
knowledge, assurance, connectors, onboarding administration) are `platform_help_staff`.
**Nothing internal is indexed**: no ADR, HLD or LLD, Terraform, runbook internals or
security material. The repository is public and the pages are shown to customers.

### Observability (ADR 0022)

Each model call is an `llm_call` row, and each agent a run: prompt hash and version,
retrieval set ids, tool calls, tokens, latency, cost, tenant and user, linked by trace
id. Spans and metrics carry the agent and task names only. No question or answer
text is logged; a failed turn logs its exception type.

## Consequences

- **Order of deployment.** Apply `04b-rbac` (`api_openai_user`) before the image bump.
  Until then the API starts, and assistant turns fail as "unavailable".
- **Customers' own documents are not searched.** `cloudops_customer` has no grant on
  `knowledge_chunk` or `document` (ADR 0020 left the knowledge screens staff-only).
  Whether a customer's assistant may cite their own SharePoint documents is a later
  decision, together with which close notes count as internal commentary.
- **The re-ranker** (ADR 0005 left it unbuilt) is on for the assistant only. If it
  fails, the fused order stands.
- **Rolling summarisation** is not built: history is truncated, not summarised.
  The 20-turn ceiling ends a conversation first.
- **The golden set's replies are written, not recorded.** They prove the pipeline: the
  guards, scope, tools and checks. Re-record them from the deployed models before
  switching a customer tenant on (D1).
- **`assistant_turn` is not partitioned.** The LLD partitions `message` monthly; at
  90 days' retention and staff volumes a plain table with a purge is enough. Revisit
  with customer volume.
- **Migration numbering.** `0023` follows `0020`. Open PR #62 holds `0021` and
  `0022`; whichever merges second renumbers its `down_revision`.
