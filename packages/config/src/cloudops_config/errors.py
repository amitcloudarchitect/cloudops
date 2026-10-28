"""Errors raised by the configuration layer.

Every one of them is fatal at startup by design: a service that cannot resolve
its configuration must not start, and must never fall back to another
environment's backends. Messages name the variable or secret that is missing,
never a value.
"""


class ConfigurationError(Exception):
    """Configuration is missing, malformed or contradictory."""


class MissingCredentialError(ConfigurationError):
    """The Azure workload identity credential is absent or unusable."""


class MissingSecretError(ConfigurationError):
    """A named secret is not in the secret store."""


class NotBuiltError(ConfigurationError):
    """The backend exists in the design but not yet in code, or is undecided."""
