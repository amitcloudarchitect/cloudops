// Mints a dev token per role with the local issuer (cloudops_devdata.tokens). The
// tokens are good only against the API on this machine and expire in an hour.
import { execFileSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";

import { ROLES, TOKEN_DIR } from "./roles";

export default function globalSetup(): void {
  const repo = new URL("../../../", import.meta.url).pathname;
  mkdirSync(TOKEN_DIR, { recursive: true });
  for (const [role, principal] of Object.entries(ROLES)) {
    const token = execFileSync("uv", ["run", "--quiet", "python", "-m", "cloudops_devdata.tokens", "--principal", principal], {
      cwd: repo,
      env: { ...process.env, CLOUDOPS_ENV: "local", CLOUDOPS_LOCAL_SECRETS_FILE: `${repo}.env.local` },
      encoding: "utf8",
    }).trim();
    writeFileSync(new URL(`${role}.txt`, TOKEN_DIR), token, { mode: 0o600 });
  }
}
