# @cloudops/contracts

The API's types for TypeScript, generated, never hand-written (CLAUDE.md, ADR 0014).

| File | Made by |
|---|---|
| `openapi.json` | `uv run python -m cloudops_api.schema > packages/contracts/openapi.json` |
| `src/api.ts` | `npm run generate -w @cloudops/contracts` (openapi-typescript) |
| `src/index.ts` | by hand, but only aliases of `src/api.ts` |

Change the API, then run both commands and commit the results. Two checks fail
otherwise: `services/api/tests/test_api_schema.py` (the JSON no longer matches the
API) and the CI web job's `npm run check` (the TypeScript no longer matches the JSON).
