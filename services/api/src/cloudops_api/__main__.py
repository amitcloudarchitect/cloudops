"""Run the API.

    CLOUDOPS_ENV=local python -m cloudops_api

Everything comes from the configuration layer; a missing setting stops the
process before it listens.
"""

from __future__ import annotations

import os
import sys
from typing import Final

import uvicorn

from cloudops_api.app import create_app
from cloudops_api.auth import KeySource, RemoteKeys, StaticKeys, TokenValidator
from cloudops_config import Component, ConfigurationError, DbLogin, Environment, load_settings
from cloudops_tenancy import Database

# The ingress proxies (Envoy, ADR 0011) keep idle connections to the API for up to an
# hour. If the API closes one first, a request already on its way over it fails as a
# 503 (Envoy flag UC). So the API must outlast the proxy: uvicorn's default is 5 s.
ENVOY_UPSTREAM_IDLE_SECONDS: Final = 3600
KEEP_ALIVE_SECONDS: Final = ENVOY_UPSTREAM_IDLE_SECONDS + 60


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
        web_origins=auth.web_origins,
    )
    uvicorn.run(
        app,
        host="127.0.0.1" if local else "0.0.0.0",  # noqa: S104
        port=8000,
        timeout_keep_alive=KEEP_ALIVE_SECONDS,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
