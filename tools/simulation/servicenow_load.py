"""Seed the simulation into a ServiceNow personal developer instance (dev only).

    I=https://devNNNNNN.service-now.com    # your personal developer instance
    uv run python tools/simulation/servicenow_load.py --instance $I --dry-run
    uv run python tools/simulation/servicenow_load.py --instance $I --auth oauth
    uv run python tools/simulation/servicenow_load.py --instance $I --auth basic --purge

A test-data tool, not platform code. It never runs in the cluster, and the
platform never calls it. It writes the same records as the System Import Sets
files (``docs/simulation``), which remain the fallback, through the Table API:

- **Order:** company, groups, CIs, then incidents, changes, change tasks,
  requested items and project tasks. A record's references are resolved to the
  sys_ids the earlier tables got, so nothing depends on display-value matching.
- **Idempotent:** every record carries the marker ``SIM-AZSPONSOR``, as the
  prefix of its ``correlation_id`` (tasks, CIs) or in its notes or description
  (the company and groups, which have no correlation_id). A run first reads the
  marked records, then updates those it finds and creates the rest. A company
  or group of the same name *without* the marker is someone else's: it is used
  for references and never changed or deleted.
- **``--purge``** deletes only marked records, newest table first.
- **``--dry-run``** makes no request and needs no credential: it reports what a
  run would send.
- **Throttled:** at most one request per ``--interval`` seconds, honouring 429
  and 503 with ``Retry-After``.
- **Logs counts only:** no record text, no number, no secret, no token.
  Errors carry the table and the HTTP status.

Credentials are a separate *seeding* account, never the platform's read-only
client (ADR 0009) and never an administrator's password. They come from the
local secret store (``.env.local``):

- ``--auth oauth``: ``SERVICENOW_SEED_CLIENT_ID`` and ``SERVICENOW_SEED_CLIENT_SECRET``,
  client credentials at ``/oauth_token.do``;
- ``--auth basic``: ``SERVICENOW_SEED_USER`` and ``SERVICENOW_SEED_PASSWORD``.

It refuses to run outside the local profile, against any host that is not a
personal developer instance (``devNNNNNN.service-now.com``), with the platform's
client id, or as a user called ``admin``.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import os
import re
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

import httpx

from cloudops_config import ConfigurationError, Environment, load_settings
from cloudops_config.secrets import SecretStore
from cloudops_config.settings import SERVICENOW_CLIENT_ID_VAR
from cloudops_connectors.servicenow.auth import ClientCredentials, TokenError
from cloudops_devdata.simulation.model import (
    DEFAULT_ESTATE,
    SIM_MARKER,
    Simulation,
    build,
    load_estate,
)
from cloudops_devdata.simulation.servicenow import import_files

PDI_HOST: Final = re.compile(r"^dev\d{4,7}\.service-now\.com$")
SEED_CLIENT_ID: Final = "servicenow-seed-client-id"
SEED_CLIENT_SECRET: Final = "servicenow-seed-client-secret"  # noqa: S105 - a name
SEED_USER: Final = "servicenow-seed-user"
SEED_PASSWORD: Final = "servicenow-seed-password"  # noqa: S105 - a name
RETRY_STATUSES: Final = frozenset({429, 503})
MAX_RETRIES: Final = 5
PAGE: Final = 500


class SeedError(Exception):
    """Carries a table and an HTTP status at most: never record text or a secret."""


@dataclass(frozen=True)
class TableSpec:
    table: str
    file: str
    # How a marked record is found, and the key that identifies it within a run.
    marker_query: str
    key: str
    # CSV column -> referenced table; the value is replaced by that record's sys_id.
    refs: Mapping[str, str] = field(default_factory=dict)
    # Columns not sent: people are invented, and are not users on the instance.
    drop: frozenset[str] = frozenset({"assigned_to"})
    optional: bool = False  # skipped when the table does not exist (PPM inactive)


_TASK_REFS: Final = {"company": "core_company", "assignment_group": "sys_user_group",
                     "cmdb_ci": "cmdb_ci_vm_instance"}
SPECS: Final[tuple[TableSpec, ...]] = (
    TableSpec("core_company", "01_core_company.csv", f"notesLIKE{SIM_MARKER}", "name"),
    TableSpec("sys_user_group", "02_sys_user_group.csv", f"descriptionLIKE{SIM_MARKER}", "name"),
    TableSpec("cmdb_ci_vm_instance", "03_cmdb_ci_vm_instance.csv",
              f"correlation_idSTARTSWITH{SIM_MARKER}-", "correlation_id",
              {"company": "core_company"}),
    TableSpec("incident", "04_incident.csv", f"correlation_idSTARTSWITH{SIM_MARKER}-",
              "correlation_id", _TASK_REFS),
    TableSpec("change_request", "05_change_request.csv",
              f"correlation_idSTARTSWITH{SIM_MARKER}-", "correlation_id", _TASK_REFS),
    TableSpec("change_task", "06_change_task.csv", f"correlation_idSTARTSWITH{SIM_MARKER}-",
              "correlation_id", {**_TASK_REFS, "change_request": "change_request"}),
    TableSpec("sc_req_item", "07_sc_req_item.csv", f"correlation_idSTARTSWITH{SIM_MARKER}-",
              "correlation_id", {"company": "core_company", "assignment_group": "sys_user_group",
                                 "configuration_item": "cmdb_ci_vm_instance"}),
    TableSpec("pm_project_task", "08_pm_project_task.csv",
              f"correlation_idSTARTSWITH{SIM_MARKER}-", "correlation_id",
              {"company": "core_company", "assignment_group": "sys_user_group"},
              optional=True),
)
# What a reference column's value is matched on, in the referenced table.
_REF_KEY: Final = {"core_company": "name", "sys_user_group": "name",
                   "cmdb_ci_vm_instance": "name", "change_request": "number"}


@dataclass
class Counts:
    created: int = 0
    updated: int = 0
    deleted: int = 0
    skipped: int = 0

    def __str__(self) -> str:
        return ", ".join(f"{k} {v}" for k, v in vars(self).items() if v) or "nothing"


class Throttle:
    """At most one request per ``interval`` seconds."""

    def __init__(self, interval: float, *,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.interval, self._sleep, self._clock = interval, sleep, clock
        self._next = 0.0

    async def wait(self) -> None:
        now = self._clock()
        if now < self._next:
            await self._sleep(self._next - now)
            now = self._next
        self._next = now + self.interval

    async def backoff(self, seconds: float) -> None:
        await self._sleep(seconds)


def records(sim: Simulation) -> dict[str, list[dict[str, str]]]:
    """The import files' rows, by table: one source for both paths."""
    files = import_files(sim)
    return {spec.table: list(csv.DictReader(io.StringIO(files[spec.file]))) for spec in SPECS}


class Instance:
    """The Table API, throttled. Every failure becomes a ``SeedError`` naming the
    table and status only."""

    def __init__(self, client: httpx.AsyncClient, throttle: Throttle) -> None:
        self.client, self.throttle = client, throttle
        self.requests = 0

    async def _send(self, method: str, table: str, path: str = "", **kw: Any) -> httpx.Response:
        url = f"/api/now/table/{table}{path}"
        for attempt in range(MAX_RETRIES + 1):
            await self.throttle.wait()
            self.requests += 1
            try:
                response = await self.client.request(method, url, **kw)
            except TokenError as exc:
                raise SeedError(f"{table}: {exc}") from None
            except httpx.HTTPError as exc:
                raise SeedError(f"{table}: {method} failed ({type(exc).__name__})") from None
            if response.status_code in RETRY_STATUSES and attempt < MAX_RETRIES:
                after = response.headers.get("Retry-After", "")
                await self.throttle.backoff(float(after) if after.isdigit() else 2.0 ** attempt)
                continue
            return response
        raise SeedError(f"{table}: {method} still throttled after {MAX_RETRIES} retries")

    async def exists(self, table: str) -> bool:
        r = await self._send("GET", table, params={"sysparm_limit": "1",
                                                   "sysparm_fields": "sys_id"})
        if r.status_code in (400, 404):
            return False
        _check(r, table, "GET")
        return True

    async def find(self, table: str, query: str, fields: Sequence[str]) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        offset = 0
        while True:
            r = await self._send("GET", table, params={
                "sysparm_query": query, "sysparm_fields": ",".join(fields),
                "sysparm_limit": str(PAGE), "sysparm_offset": str(offset),
                "sysparm_exclude_reference_link": "true"})
            _check(r, table, "GET")
            page = r.json().get("result", [])
            out.extend({k: str(v) for k, v in row.items()} for row in page)
            if len(page) < PAGE:
                return out
            offset += PAGE

    async def create(self, table: str, body: Mapping[str, str]) -> str:
        r = await self._send("POST", table, json=dict(body),
                             params={"sysparm_input_display_value": "false"})
        _check(r, table, "POST")
        return str(r.json()["result"]["sys_id"])

    async def update(self, table: str, sys_id: str, body: Mapping[str, str]) -> None:
        r = await self._send("PATCH", table, f"/{sys_id}", json=dict(body),
                             params={"sysparm_input_display_value": "false"})
        _check(r, table, "PATCH")

    async def delete(self, table: str, sys_id: str) -> None:
        r = await self._send("DELETE", table, f"/{sys_id}")
        if r.status_code != 404:  # already gone is fine
            _check(r, table, "DELETE")


def _check(r: httpx.Response, table: str, method: str) -> None:
    if r.status_code >= 400:
        # The body may echo field values: only the status leaves this function.
        raise SeedError(f"{table}: {method} refused with HTTP {r.status_code}")


def _body(row: Mapping[str, str], spec: TableSpec,
          ids: Mapping[str, Mapping[str, str]]) -> dict[str, str]:
    body: dict[str, str] = {}
    for column, value in row.items():
        if column in spec.drop or value == "":
            continue
        if column in spec.refs:
            target = spec.refs[column]
            sys_id = ids[target].get(value)
            if sys_id is None:
                raise SeedError(f"{spec.table}: a {column} reference has no record in {target}")
            body[column] = sys_id
        else:
            body[column] = value
    return body


async def seed(instance: Instance, sim: Simulation) -> dict[str, Counts]:
    rows = records(sim)
    ids: dict[str, dict[str, str]] = {}  # table -> reference key -> sys_id
    counts: dict[str, Counts] = {}
    for spec in SPECS:
        c = counts[spec.table] = Counts()
        if spec.optional and not await instance.exists(spec.table):
            c.skipped = len(rows[spec.table])
            continue
        ref_key = _REF_KEY.get(spec.table, "number")
        fields = sorted({"sys_id", spec.key, ref_key})
        marked = {r[spec.key]: r for r in await instance.find(spec.table, spec.marker_query,
                                                              fields)}
        ids[spec.table] = {r[ref_key]: r["sys_id"] for r in marked.values()}
        if spec.table in ("core_company", "sys_user_group"):
            # Same name, no marker: someone else's record. Reference it, never touch it.
            names = "^OR".join(f"name={r['name']}" for r in rows[spec.table])
            for r in await instance.find(spec.table, names, ["sys_id", "name"]):
                if r["name"] not in marked:
                    ids[spec.table][r["name"]] = r["sys_id"]
                    c.skipped += 1
        for row in rows[spec.table]:
            key = row[spec.key]
            if key not in marked and key in ids[spec.table]:
                continue  # the unmarked record adopted above
            body = _body(row, spec, ids)
            if key in marked:
                await instance.update(spec.table, marked[key]["sys_id"], body)
                c.updated += 1
            else:
                ids[spec.table][row[ref_key]] = await instance.create(spec.table, body)
                c.created += 1
    return counts


async def purge(instance: Instance) -> dict[str, Counts]:
    counts: dict[str, Counts] = {}
    for spec in reversed(SPECS):
        c = counts[spec.table] = Counts()
        if spec.optional and not await instance.exists(spec.table):
            continue
        for r in await instance.find(spec.table, spec.marker_query, ["sys_id"]):
            await instance.delete(spec.table, r["sys_id"])
            c.deleted += 1
    return counts


def dry_run(sim: Simulation) -> dict[str, Counts]:
    return {table: Counts(created=len(rows)) for table, rows in records(sim).items()}


# Credentials and guards -----------------------------------------------------------


def check_instance(url: str) -> str:
    parts = urlsplit(url.rstrip("/"))
    if parts.scheme != "https" or not PDI_HOST.match(parts.netloc) or parts.path:
        raise SeedError("--instance must be a personal developer instance, "
                        "https://devNNNNNN.service-now.com, with no path")
    return f"https://{parts.netloc}"


def auth_for(mode: str, instance: str, store: SecretStore,
             environ: Mapping[str, str]) -> httpx.Auth:
    if mode == "oauth":
        client_id = store.get(SEED_CLIENT_ID)
        if client_id == environ.get(SERVICENOW_CLIENT_ID_VAR, "").strip():
            raise SeedError("the seed client id is the platform's read-only client; "
                            "create a separate seeding client (ADR 0009)")
        return ClientCredentials(f"{instance}/oauth_token.do", client_id,
                                 lambda: store.get(SEED_CLIENT_SECRET))
    user = store.get(SEED_USER)
    if user.strip().lower() == "admin":
        raise SeedError("refusing the admin account; use a dedicated seeding user")
    return httpx.BasicAuth(user, store.get(SEED_PASSWORD))


def _print(action: str, counts: Mapping[str, Counts]) -> None:
    for table, c in counts.items():
        print(f"  {action} {table}: {c}")


async def _run(args: argparse.Namespace, sim: Simulation) -> int:
    settings = load_settings(os.environ, require=())
    if settings.environment is not Environment.LOCAL:
        raise SeedError("the ServiceNow seeder runs in the local profile only")
    instance = check_instance(args.instance)
    auth = auth_for(args.auth, instance, settings.secrets, os.environ)
    async with httpx.AsyncClient(base_url=instance, auth=auth, timeout=30.0,
                                 headers={"Accept": "application/json"}) as client:
        api = Instance(client, Throttle(args.interval))
        if args.purge:
            _print("purge", await purge(api))
        else:
            _print("seed", await seed(api, sim))
        print(f"  {api.requests} requests")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tools/simulation/servicenow_load.py")
    parser.add_argument("--instance", required=True,
                        help="the developer instance, https://devNNNNNN.service-now.com")
    parser.add_argument("--auth", choices=("oauth", "basic"), default="oauth")
    parser.add_argument("--anchor", type=date.fromisoformat,
                        default=datetime.now(UTC).date() - timedelta(days=1))
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--estate", type=Path, default=DEFAULT_ESTATE,
                        help=f"the subscription's VM export, local only (default {DEFAULT_ESTATE})")
    parser.add_argument("--interval", type=float, default=0.25,
                        help="seconds between requests (default 0.25)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="send nothing, report counts")
    mode.add_argument("--purge", action="store_true",
                      help=f"delete every record marked {SIM_MARKER}, and nothing else")
    args = parser.parse_args(argv)
    try:
        sim = build(args.anchor, args.seed, load_estate(args.estate))
        if args.dry_run:
            check_instance(args.instance)
            _print("would send", dry_run(sim))
            print(f"  (records marked {SIM_MARKER}; existing ones are updated, not duplicated)")
            return 0
        return asyncio.run(_run(args, sim))
    except (SeedError, ConfigurationError, FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
