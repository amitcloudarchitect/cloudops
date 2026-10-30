"""The API's OpenAPI schema: the one source of the console's types (ADR 0014).

    uv run python -m cloudops_api.schema > packages/contracts/openapi.json

``packages/contracts`` generates TypeScript from that file; nobody writes those
types by hand. A test fails when the committed file no longer matches the API.
"""

from __future__ import annotations

import json
from typing import Any

from cloudops_api.app import create_app
from cloudops_api.auth import StaticKeys, TokenValidator
from cloudops_config import AuthConfig
from cloudops_tenancy import Database

# Building the app opens nothing: connections are lazy and no request is served.
_UNUSED_DSN = "postgresql://schema:unused@127.0.0.1:1/none"


def openapi() -> dict[str, Any]:
    auth = AuthConfig("https://schema.invalid", "schema", "schema", "schema", None, None)
    app = create_app(
        db=Database.create(_UNUSED_DSN),
        validator=TokenValidator(auth, StaticKeys({"keys": []})),
        expose_openapi=True,
    )
    return app.openapi()


def render() -> str:
    return json.dumps(openapi(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    print(render(), end="")
