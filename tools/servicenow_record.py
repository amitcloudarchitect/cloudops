"""Record contract fixtures from a real ServiceNow instance, scrubbed (ADR 0009).

    uv run python tools/servicenow_record.py --instance https://dev407774.service-now.com \
        --client-id 666769454e68469d8d39fb53b61337dc

Reads one page of each connector entity with the platform's own client (OAuth
client credentials, the secret fetched from Key Vault into memory only) and
writes ``connectors/tests/recorded/<host>/<entity>.json`` in the Table API's
``display_value=all`` shape. The contract tests then prove every real record
still normalises.

Scrubbed before anything is written, so the files are safe to commit:

- every reference to a person becomes ``Person N`` with a placeholder sys_id,
  the same person getting the same N across files;
- long free text (description, comments, work notes) is replaced;
- email addresses and phone numbers are removed from the text that is kept.

Review the diff before committing: scrubbing is by field name and pattern, and
a new custom field on the instance could carry something it does not know.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cloudops_connectors.servicenow.auth import ClientCredentials
from cloudops_connectors.servicenow.client import ServiceNowClient
from cloudops_connectors.servicenow.mapping import ENTITIES

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "connectors" / "tests" / "recorded"

PERSON_FIELDS = frozenset({
    "assigned_to", "caller_id", "closed_by", "opened_by", "reopened_by", "resolved_by",
    "sys_created_by", "sys_updated_by", "watch_list", "work_notes_list", "requested_by",
    "requested_for", "approval_set", "u_requested_for",
})
REDACTED_TEXT = frozenset({"description", "comments", "work_notes", "comments_and_work_notes",
                           "user_input"})
# Free text kept but cleaned. Phone numbers are only looked for here: the pattern
# would also eat date-times like "2026-10-29 12:00:00" in structured fields.
KEPT_TEXT = frozenset({"short_description", "close_notes"})
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
_PHONE = re.compile(r"\+?\d[\d ()-]{7,}\d")


class Scrubber:
    def __init__(self) -> None:
        self._people: dict[str, int] = {}

    def _person(self, original: str) -> int:
        return self._people.setdefault(original, len(self._people) + 1)

    def _text(self, value: Any, *, free_text: bool) -> Any:
        if not isinstance(value, str):
            return value
        value = _EMAIL.sub("[email]", value)
        return _PHONE.sub("[phone]", value) if free_text else value

    def field(self, name: str, cell: Any) -> Any:
        free = name in KEPT_TEXT
        if not isinstance(cell, dict):
            return self._text(cell, free_text=free)
        value, display = cell.get("value", ""), cell.get("display_value", "")
        if name in PERSON_FIELDS:
            if not value and not display:
                return {"value": "", "display_value": ""}
            people = [p for p in str(value or display).split(",") if p]
            ns = [self._person(p) for p in people]
            return {"value": ",".join(f"{n:032x}" for n in ns),
                    "display_value": ", ".join(f"Person {n}" for n in ns)}
        if name in REDACTED_TEXT:
            filled = bool(value or display)
            return {"value": "[redacted]" if filled else "",
                    "display_value": "[redacted]" if filled else ""}
        return {**cell, "value": self._text(value, free_text=free),
                "display_value": self._text(display, free_text=free)}

    def record(self, record: dict[str, Any]) -> dict[str, Any]:
        return {k: self.field(k, v) for k, v in sorted(record.items())}


def _secret() -> str:
    az = shutil.which("az")
    if az is None:
        raise SystemExit("error: az is not installed")
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [az, "keyvault", "secret", "show", "--vault-name", "kv-cloudops-app-dev-weu",
         "--name", "servicenow-read-client-secret", "--query", "value", "-o", "tsv"],
        check=True, capture_output=True, text=True).stdout.rstrip("\n")


async def record(instance: str, client_id: str) -> list[Path]:
    host = urlsplit(instance).netloc.split(".")[0]
    client = ServiceNowClient(instance, auth=ClientCredentials(
        f"{instance}/oauth_token.do", client_id, _secret))
    scrub = Scrubber()
    written = []
    try:
        for entity in sorted(ENTITIES):
            body = json.loads(await client.fetch(entity, datetime.now(UTC) - timedelta(days=3650),
                                                 0))
            path = OUT / host / f"{entity}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"result": [scrub.record(r) for r in body["result"]]},
                                       indent=1, sort_keys=True) + "\n", encoding="utf-8")
            written.append(path)
    finally:
        await client.aclose()
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--instance", required=True)
    parser.add_argument("--client-id", required=True)
    args = parser.parse_args(argv)
    for path in asyncio.run(record(args.instance.rstrip("/"), args.client_id)):
        count = len(json.loads(path.read_text(encoding="utf-8"))["result"])
        print(f"wrote {path.relative_to(REPO)} ({count} records)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
