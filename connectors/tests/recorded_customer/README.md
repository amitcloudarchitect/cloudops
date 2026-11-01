# Customer-credential check fixtures (ADR 0027)

Response bodies in the documented shapes of the calls `customer/check.py` makes,
with synthetic identifiers. They were not recorded from a customer estate: no
customer credential existed when they were written. Replace them with recorded,
redacted responses after the first real onboarding (ids and names only; these
calls return no secret).

| File | Call |
| --- | --- |
| `azure_subscriptions.json` | ARM `GET /subscriptions` |
| `azure_role_assignments_read_only.json` | `GET .../roleAssignments?$filter=assignedTo('<oid>')`: Reader, Cost Management Reader, Monitoring Reader |
| `azure_role_assignments_contributor.json` | the same, with Contributor inherited from a group at the management group |
| `azure_role_definition_contributor.json` | `GET {roleDefinitionId}` |
| `sts_caller_identity.xml` | STS `GetCallerIdentity` (an IAM user) |
| `sts_caller_identity_root.xml` | the same, for the root user's key |
| `iam_*.xml` | IAM `List*Policies`, `ListGroupsForUser`, `GetPolicy`, `GetPolicyVersion` |
