// Prepends the do-not-edit banner to the generated file, so nobody mistakes it
// for source (ADR 0014). Idempotent.
import { readFileSync, writeFileSync } from "node:fs";

const path = new URL("../src/api.ts", import.meta.url);
const banner =
  "// GENERATED from packages/contracts/openapi.json by openapi-typescript. Do not edit.\n" +
  "// Regenerate: uv run python -m cloudops_api.schema > packages/contracts/openapi.json\n" +
  "//             npm run generate -w @cloudops/contracts\n\n";
const body = readFileSync(path, "utf8");
writeFileSync(path, body.startsWith(banner) ? body : banner + body);
