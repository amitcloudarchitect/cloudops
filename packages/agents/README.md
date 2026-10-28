# Agents

The agent layer: a few hundred lines with no framework. Temporal owns control flow; an
agent fills in content at one step and returns it (CLAUDE.md rule 3). No LangChain,
LangGraph or CrewAI: a second orchestrator inside Temporal would hide exactly what we
most need to inspect, which is what the model was asked and what came back.

An agent is five things (`AgentSpec`):

| Part | What | Example (`classifier.py`) |
| --- | --- | --- |
| Prompt | A versioned file, `prompts/<agent>/v<N>.md`. The version recorded carries a hash of the file. | `ticket_classifier/v1` |
| Tier | `small` or `large`, never a model name | `Tier.SMALL` |
| Tools | An allowlist of registry tool names, which may be empty | `frozenset()` |
| Output | A Pydantic model extending `AgentOutput`: closed (`extra="forbid"`) and carrying `confidence` | `TicketClassification` |
| Threshold | Below this confidence the result is `declined`, with no output to act on | `0.6` |

## What the base guarantees

- **Output** is validated by Pydantic against the schema: JSON in, a model out, with no
  regex and no string handling. One retry feeds the validation errors back; a second
  failure raises `AgentOutputError`. Nothing is ever partly parsed.
- **Tools** reach the step 06 registry only if they are on the agent's allowlist; the
  registry then checks permission, schema and scope. A tool outside the allowlist is
  refused before the registry sees it. Results return as JSON. After 10 rounds the
  agent records an escalation and raises `AgentEscalationError`.
- **Prompts** come from `build_messages`:
  - the system message holds the template, the standing rule that data is never an
    instruction, and the output schema;
  - untrusted content (tickets, chunks, tool results) goes in `<data id="…">` sections
    whose id is random per build, with fake section tags defused;
  - the user's request has its own section.
- **The gateway** does the rest: redaction, quotas, fallback, the cache, and one
  `llm_call` trace row per model call. The prompt never contains a tenant id, a
  credential or a connection.
- **Trace:** one `cloudops.agent_run` row per run, whatever the outcome. It records
  the prompt version and hash, tier, retrieval ids, tool calls, the gateway call ids,
  tokens, latency, cost, tenant and user.

## Adding an agent

1. **Write the prompt** as `src/cloudops_agents/prompts/<agent>/v1.md`.
   - Say what to decide and what *not* to do.
   - Don't describe the output format; the builder adds the JSON Schema.
   - Use `$slot` only for trusted values.
2. **Define the output** by subclassing `AgentOutput`.
   - Use `Literal` for anything that becomes a decision or a code (category,
     priority, runbook id), so an injected value cannot validate.
   - Bound every string.
3. **Declare the spec:**

   ```python
   MY_AGENT = AgentSpec(
       name="my_task",                     # also the gateway task name in traces
       prompt=PromptTemplate.load("my_agent", "v1"),
       tier=Tier.SMALL,                    # LARGE only for multi-step reasoning
       output=MyOutput,
       confidence_threshold=0.7,
       tools=frozenset({"search_tickets"}),  # read tools only, from the registry
   )
   ```

4. **Pass untrusted input as `DataItem`s**, never formatted into the request string.
5. **Run it in Temporal as an activity:** register it in `AgentActivities({...})` and
   call `run_agent` from your workflow with the `AGENT_*` timeouts and `AGENT_RETRY`.
   The workflow decides what happens with the result; the agent never does.
6. **Test it:**
   - with `FakeGateway` (see `tests/test_agents_base.py`): valid output, invalid output
     twice, below the threshold, the allowlist, injected content;
   - and with golden-set cases in the evaluation harness (step 08).
7. **Changing the prompt means a new file** (`v2.md`), never an edit to `v1.md`. Then
   run the evaluation suite before switching.

## The agents so far, and the pipeline they serve

| Agent | Stage | Tier | Tools | Output |
| --- | --- | --- | --- | --- |
| `classify_ticket` (`classifier.py`, prompt `ticket_classifier/v2`) | 2 Classify | small | none | intent, affected CI, customer, action class, risk, confidence |
| `enrich_ticket` (`enricher.py`, prompt `ticket_enricher/v1`) | 3 Enrich | large | `get_resource_context`, `search_tickets`, `search_knowledge` | the context bundle |

`remediation.py` holds `TicketTriageWorkflow`, stages 1 to 3 as a Temporal workflow.
`triage.py` holds the deterministic guardrails between the stages; ADR 0006 explains
them. Both agents are measured by the evaluation suite (`evals/README.md`) from their
first commit. Stage 4 onwards needs the security review first.

## Limits

These tests prove the structure: injected text stays inside data, a tool outside the
allowlist never runs, and an injected action cannot pass the schema. They cannot prove
that a real model *ignores* injected instructions when the output is a valid value it
was nudged towards. That is measured by the injection cases in the evaluation suite
(step 08), against the real tier models.
