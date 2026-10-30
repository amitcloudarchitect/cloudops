# Runbooks

Signed, versioned manifests the execution plane runs (LLD, Runbook manifest). The
agent selects and parameterises one; it never composes commands.

Empty until the execution step, which waits for the security review (CLAUDE.md).
The pipeline already validates anything placed here
(`tools/ci/check_runbooks.py`, `.github/workflows/runbooks.yml`): schema, bounded
parameters, no interpolation, rollback for class C and above, and no signature in
source. Only the pipeline signs, on the protected branch; dry-run, signing and
publication fail closed until the security review (ADR 0013).
