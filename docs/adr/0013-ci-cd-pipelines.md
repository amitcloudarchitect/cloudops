# ADR 0013: CI/CD pipelines

- **Status:** Accepted, 2026-10-30
- **Decided by:** Amit Kumar
- **Implements:** LLD, CI/CD and Delivery; step 13. `.github/workflows/{ci,main,promote,runbooks}.yml`,
  `.github/CODEOWNERS`, `tools/ci/` (gates and their tests), `policy/terraform/`,
  stacks 02 and 04b (the CI identity). Setup steps: `docs/ci.md`.

## Decisions

1. **One gate workflow, `ci.yml`, run on every pull request and as the first half of
   the main pipeline.** Each LLD stage is its own job, so a failure names its gate:
   - data access outside `packages/tenancy`; monetary literals outside the rate card;
   - secret scan (gitleaks, whole history);
   - ruff, mypy `--strict`, runbook validation, and tsc once `apps/web` has a lockfile;
   - unit, contract (recorded fixtures) and integration tests, then coverage of 80% on
     tenancy and calculation code, then the isolation suite;
   - the AI evaluation suite against its baseline;
   - the pricing reference case;
   - SAST (CodeQL, security-extended) and SCA (Grype over `uv.lock` and every
     manifest), failing on high and critical;
   - every image built, SBOM'd, checked for non-root, and scanned (critical/high);
   - Terraform fmt and validate on every stack, `conftest verify` on the policies,
     then a real read-only plan per stack with the tag check and `conftest test`.
   Third-party actions are pinned to commit SHAs, tools to versions and checksums;
   a test fails on an unpinned action or a widened default token.
2. **The CI identity pushes and reads; it never applies.** `id-cloudops-cicd-dev` (LLD:
   `id-cloudops-cicd`, federated to GitHub OIDC, no secret) trusts exactly two
   subjects: `main`, and this repository's pull requests (GitHub gives forks no
   token). It holds AcrPush on the registry, Reader on each platform resource group
   (never the subscription) and read on the Terraform state container. Plans run
   with `-lock=false`, so they take no lease and write nothing. It is not in the
   workload set that reads the app vault's secrets, by construction (04b). Applying
   Terraform stays with a person (CLAUDE.md).
3. **The main pipeline (`main.yml`)** runs every gate, then builds the images once,
   scans those exact images, pushes them, and signs each by digest. A tag bump touches
   only the dev overlays, which the pipeline ignores, so it never loops.
4. **Keyless signing** (Sigstore, chosen over a Key Vault key): each signature binds
   the digest to `main.yml` on `main` in this repository through GitHub's OIDC
   identity, recorded in Sigstore's public transparency log (the repository is public
   already). Promotion verifies it. Runbook signing is separate: the LLD's
   HSM-backed `key-runbook-signing`, with the execution plane.
5. **A GitHub App release bot moves dev** (chosen over a person merging each bump).
   After a release it opens a pull request setting dev's image tags (with
   `tools/ci/set_image_tag.py`, which edits only those lines), and enables auto-merge.
   Its pull request runs every check like anyone's; it is the main ruleset's only
   automated bypass actor, so it merges without a human review. Its private key is
   the one stored CI credential: a GitHub Actions secret, rotated with the App.
6. **Promotion (`promote.yml`)** is started by hand from `main` with the tag dev has
   proven. It checks the request (production needs a `CHG` change record), verifies
   every image's signature, waits for the environment's required reviewers, then has
   the bot open the overlay pull request. It fails closed until UAT and production
   exist: their overlays list no images and their registries are not built.
7. **The runbook path (`runbooks.yml`)** validates every manifest now
   (`tools/ci/check_runbooks.py`: closed schema, bounded parameters, no
   interpolation, rollback for class C and above, a post-condition, and no signature
   in source, since only the pipeline signs). Dry-run, signing and publication run
   only on `main` and fail closed with the reason until the execution plane is
   reviewed (CLAUDE.md; LLD L7 sandbox accounts, L9 signing ceremony).
8. **Main is pull-request only**, with required checks, a required review and code
   owners on `packages/tenancy`, `execution/`, `runbooks/`, `infra/`, `policy/`,
   `tools/ci/` and `.github/` (decided). Settings are in `docs/ci.md`.
9. **The rate-card gate** treats any `Decimal` literal in product source as money,
   except 0, 1 and powers of ten (precision and unit constants). Exceptions are the
   rate cards in `tools/ci/money_literal_allowlist.txt` and lines marked
   `# not money: <reason>`. It moved the spend breaker's $5 floor into the AI rate
   card on its first run.

## Open

- **Pricing regression is not exact yet.** The only reference case is
  `reference/pricing_engine.py`, which compares floats within a tolerance. The LLD's
  exact gate arrives with the `Decimal` commercial engine (step 16), which must also
  join the coverage gate's file list (`pyproject.toml`).
- **One human.** GitHub never lets an author approve their own pull request, so
  until a second reviewer exists the owner merges through the ruleset's
  pull-request-only admin bypass (`docs/ci.md`). The LLD's "peer review by someone
  other than the author" for runbooks needs that second person before any runbook
  is merged.
- **Promotion into other registries** (LLD: production pulls only from
  `crcloudopsprdweu`): a signed copy step, built with the UAT and production
  environments.

## First run (2026-10-30, `bddb3a6`)

The gates stopped the release, as designed; nothing was pushed or signed.

- **Secret scan:** three redaction-test fixtures (a sample JWT and invented keys) in
  `services/gateway/tests/test_gateway_units.py`. `.gitleaks.toml` excepts those exact
  values in that file only; a different key there still fails.
- **SAST:** two false positives (a Key Vault secret's *name* printed by
  `scripts/db_dev_logins.py`; the field is now `vault_entry`) and one finding kept by
  design: `python -m cloudops_config secret` prints a local `.env.local` value for the
  Makefile, and refuses outside the local profile. Exceptions are file-and-rule
  entries in `tools/ci/sast_exceptions.txt`, checked by `tools/ci/check_sarif.py`;
  the finding stays visible in code scanning.
- **Images:** the Anchore download actions do not put `syft` and `grype` on `PATH`;
  both workflows now add them.
- **OIDC subject:** GitHub issues this repository's tokens in its immutable form,
  `repo:amitcloudarchitect@91311714/cloudops@1437077070:...`, which a re-registered
  name cannot reuse. Stack 02 trusts that form (and keeps the by-name form, so a
  change of GitHub's setting cannot lock CI out).
- **Refresh needs more than Reader for two stacks:** refreshing Redis lists its access
  keys, and refreshing AKS fetches a kubeconfig. Rather than give CI those rights,
  `04-data` and `05-aks` plan with `-refresh=false` (a dev relaxation): every change
  is still checked against state, but drift made outside Terraform is not seen there.
  The LLD's nightly drift detection must use an identity allowed to refresh them.
- The `conftest` policy unit tests passed (the devbox's permission rules had refused
  to run a downloaded `conftest`, so CI was their first run).
