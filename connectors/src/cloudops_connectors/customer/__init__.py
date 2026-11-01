"""Customer estates read with the customer's own stored credential (ADR 0003, ADR 0027).

Until production, a customer's Azure subscriptions may be read with an app
registration's client secret, and an AWS account with an IAM user's access key,
read-only, under ADR 0003's six guardrails. Production uses delegated access
through the broker and none of this: ``cloudops_config`` refuses ``stored_key``
when the deployment is ``prd``.

- ``credentials``: the secret, read by name from the ``SecretStore`` at each use.
- ``check``: the read-only validation and role check an onboarding asks for.
- ``sources``: the Azure and AWS sources bound to one credential: one tenant, its
  accounts only.
- ``worker``: claims checks, then runs one Temporal worker and schedule set per
  active credential, and the first sync.
"""
