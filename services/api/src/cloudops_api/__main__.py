"""Run the API.

    CLOUDOPS_ENV=local python -m cloudops_api

Everything comes from the configuration layer; a missing setting stops the
process before it listens.
"""

from __future__ import annotations

import os
import sys

import uvicorn

from cloudops_api.app import create_app
from cloudops_api.auth import KeySource, RemoteKeys, StaticKeys, TokenValidator
from cloudops_config import Component, ConfigurationError, DbLogin, Environment, load_settings
from cloudops_tenancy import Database


def main() -> int:
    try:
        settings = load_settings(os.environ, require={Component.DATABASE_APP, Component.AUTH})
    except ConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    auth = settings.auth
    keys: KeySource
    if auth.jwks_secret is not None:
        keys = StaticKeys.from_json(settings.secrets.get(auth.jwks_secret))
    elif auth.jwks_url is not None:
        keys = RemoteKeys(auth.jwks_url)
    else:
        print("error: no token key source configured", file=sys.stderr)
        return 1
    local = settings.environment is Environment.LOCAL
    app = create_app(
        db=Database.create(settings.database_dsn(DbLogin.APP)),
        validator=TokenValidator(auth, keys),
        expose_openapi=local,
    )
    uvicorn.run(app, host="127.0.0.1" if local else "0.0.0.0", port=8000)  # noqa: S104
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
