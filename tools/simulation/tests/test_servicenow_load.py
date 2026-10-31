"""The ServiceNow seeder, against an in-memory Table API (httpx.MockTransport).
Nothing here reaches an instance: until the seeding account exists, this fake is
the only thing the seeder has talked to."""

from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

from cloudops_config.secrets import EnvFileSecretStore
from cloudops_devdata.simulation.model import SIM_MARKER, build, example_estate

REPO = Path(__file__).resolve().parents[3]
SIM = build(date(2026, 10, 30), 11, example_estate())
SECRET = "s3ed-secret-value"  # a test value
URL = "https://dev1234567.service-now.com"  # an invented developer instance


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "servicenow_load", REPO / "tools/simulation/servicenow_load.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["servicenow_load"] = module
    spec.loader.exec_module(module)
    return module


sl = _load()


class FakeInstance:
    """Enough of the Table API for the seeder: list with the query operators it
    uses, create, update, delete, a token endpoint, and injected throttling."""

    def __init__(self, *, ppm: bool = True, throttle_first: int = 0) -> None:
        self.tables: dict[str, dict[str, dict[str, str]]] = {
            t: {} for t in ("core_company", "sys_user_group", "cmdb_ci_vm_instance", "incident",
                            "change_request", "change_task", "sc_req_item")}
        if ppm:
            self.tables["pm_project_task"] = {}
        self.throttle_first = throttle_first
        self.log: list[tuple[str, str]] = []
        self.auth: list[str] = []

    @staticmethod
    def _match(row: dict[str, str], query: str) -> bool:
        if not query:
            return True
        for clause in query.split("^OR"):
            for op in ("STARTSWITH", "LIKE", "="):
                if op in clause:
                    name, value = clause.split(op, 1)
                    have = row.get(name, "")
                    ok = (have.startswith(value) if op == "STARTSWITH" else
                          value in have if op == "LIKE" else have == value)
                    if ok:
                        return True
                    break
        return False

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/oauth_token.do":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 1800})
        self.auth.append(request.headers.get("Authorization", ""))
        self.log.append((request.method, path))
        if self.throttle_first > 0:
            self.throttle_first -= 1
            return httpx.Response(429, headers={"Retry-After": "1"})
        parts = path.split("/")  # /api/now/table/<table>[/<sys_id>]
        table = parts[4]
        if table not in self.tables:
            return httpx.Response(400, json={"error": {"message": "Invalid table"}})
        store = self.tables[table]
        if request.method == "GET":
            q = request.url.params
            rows = [r for r in store.values() if self._match(r, q.get("sysparm_query", ""))]
            offset, limit = int(q.get("sysparm_offset", 0)), int(q.get("sysparm_limit", 10000))
            return httpx.Response(200, json={"result": rows[offset:offset + limit]})
        if request.method == "POST":
            row = {k: str(v) for k, v in json.loads(request.content).items()}
            row["sys_id"] = uuid.uuid4().hex
            store[row["sys_id"]] = row
            return httpx.Response(201, json={"result": row})
        sys_id = parts[5]
        if request.method == "PATCH":
            store[sys_id].update({k: str(v) for k, v in json.loads(request.content).items()})
            return httpx.Response(200, json={"result": store[sys_id]})
        store.pop(sys_id, None)
        return httpx.Response(204)


async def _no_sleep(_: float) -> None:
    return None


def _api(fake: FakeInstance, auth: httpx.Auth | None = None) -> Any:
    client = httpx.AsyncClient(base_url=URL, transport=httpx.MockTransport(fake.handler),
                               auth=auth or httpx.BasicAuth("seeder", SECRET))
    return sl.Instance(client, sl.Throttle(0, sleep=_no_sleep))


def _expected() -> dict[str, int]:
    return {t: len(rows) for t, rows in sl.records(SIM).items()}


async def test_a_run_creates_every_record_in_dependency_order() -> None:
    fake = FakeInstance()
    counts = await sl.seed(_api(fake), SIM)
    assert {t: c.created for t, c in counts.items()} == _expected()
    assert {t: len(fake.tables[t]) for t in _expected()} == _expected()
    first_post: dict[str, int] = {}
    for i, (method, path) in enumerate(fake.log):
        if method == "POST":
            first_post.setdefault(path.split("/")[4], i)
    order = [s.table for s in sl.SPECS]
    assert sorted(first_post, key=first_post.__getitem__) == order


async def test_references_are_sys_ids_of_the_records_just_created() -> None:
    fake = FakeInstance()
    await sl.seed(_api(fake), SIM)
    company = next(iter(fake.tables["core_company"]))
    cis = fake.tables["cmdb_ci_vm_instance"]
    changes = fake.tables["change_request"]
    for row in fake.tables["incident"].values():
        assert row["company"] == company
        assert row.get("cmdb_ci", next(iter(cis))) in cis
        assert "assigned_to" not in row, "invented people are not users on the instance"
    for row in fake.tables["change_task"].values():
        assert row["change_request"] in changes
    marked = [r["correlation_id"] for t in ("incident", "cmdb_ci_vm_instance")
              for r in fake.tables[t].values()]
    assert all(m.startswith(f"{SIM_MARKER}-") for m in marked)


async def test_a_second_run_updates_and_duplicates_nothing() -> None:
    fake = FakeInstance()
    await sl.seed(_api(fake), SIM)
    again = await sl.seed(_api(fake), SIM)
    assert {t: c.created for t, c in again.items() if c.created} == {}
    assert {t: c.updated for t, c in again.items()} == _expected()
    assert {t: len(fake.tables[t]) for t in _expected()} == _expected()


async def test_an_unmarked_company_of_the_same_name_is_used_and_never_touched() -> None:
    fake = FakeInstance()
    theirs = {"sys_id": "c0ffee", "name": "Azure Sponsorship", "notes": "made by hand"}
    fake.tables["core_company"]["c0ffee"] = dict(theirs)
    await sl.seed(_api(fake), SIM)
    assert fake.tables["core_company"] == {"c0ffee": theirs}
    assert all(r["company"] == "c0ffee" for r in fake.tables["incident"].values())
    await sl.purge(_api(fake))
    assert fake.tables["core_company"] == {"c0ffee": theirs}


async def test_purge_deletes_only_marked_records() -> None:
    fake = FakeInstance()
    await sl.seed(_api(fake), SIM)
    real = {"sys_id": "abc", "number": "INC0010001", "correlation_id": "",
            "short_description": "a real ticket"}
    ci = {"sys_id": "def", "name": "vm-dc-ea-01", "correlation_id": "discovery-1"}
    fake.tables["incident"]["abc"] = real
    fake.tables["cmdb_ci_vm_instance"]["def"] = ci
    counts = await sl.purge(_api(fake))
    assert {t: c.deleted for t, c in counts.items()} == _expected()
    assert fake.tables["incident"] == {"abc": real}
    assert fake.tables["cmdb_ci_vm_instance"] == {"def": ci}
    assert all(not rows for t, rows in fake.tables.items()
               if t not in ("incident", "cmdb_ci_vm_instance"))


async def test_a_missing_project_task_table_is_skipped() -> None:
    fake = FakeInstance(ppm=False)
    counts = await sl.seed(_api(fake), SIM)
    assert counts["pm_project_task"].skipped == _expected()["pm_project_task"]
    assert counts["pm_project_task"].created == 0


async def test_throttling_is_honoured_with_retry_after() -> None:
    fake = FakeInstance(throttle_first=2)
    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)

    clock = iter(float(i) / 100 for i in range(100_000))
    client = httpx.AsyncClient(base_url=URL, transport=httpx.MockTransport(fake.handler),
                               auth=httpx.BasicAuth("seeder", SECRET))
    api = sl.Instance(client, sl.Throttle(0.25, sleep=sleep, clock=lambda: next(clock)))
    await sl.seed(api, SIM)
    assert slept[:1] == [1.0], "Retry-After honoured"
    assert len(slept) > len(fake.log) / 2, "requests are spaced by the interval"


async def test_a_refusal_carries_the_status_and_no_record_text() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        body = request.content.decode() or "nothing"
        return httpx.Response(403, json={"error": {"message": f"denied: {body}"}})

    client = httpx.AsyncClient(base_url=URL, transport=httpx.MockTransport(refuse))
    with pytest.raises(sl.SeedError) as err:
        await sl.seed(sl.Instance(client, sl.Throttle(0, sleep=_no_sleep)), SIM)
    assert str(err.value) == "core_company: GET refused with HTTP 403"


def test_dry_run_sends_nothing_and_logs_counts_only(capsys: pytest.CaptureFixture[str]) -> None:
    example = REPO / "packages/devdata/src/cloudops_devdata/simulation/example_estate.csv"
    assert sl.main(["--instance", URL, "--anchor", "2026-10-30", "--estate", str(example),
                    "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would send incident: created" in out
    texts = [r["short_description"] for r in sl.records(SIM)["incident"]]
    numbers = [r["number"] for r in sl.records(SIM)["incident"]]
    assert not any(t in out for t in texts)
    assert not any(n in out for n in numbers)


async def test_oauth_uses_the_seed_client_and_never_the_platforms() -> None:
    store = EnvFileSecretStore({"SERVICENOW_SEED_CLIENT_ID": "seed-client",
                                "SERVICENOW_SEED_CLIENT_SECRET": SECRET})
    auth = sl.auth_for("oauth", URL, store, {"CLOUDOPS_SERVICENOW_CLIENT_ID": "read-client"})
    fake = FakeInstance()
    await sl.seed(_api(fake, auth), SIM)
    assert set(fake.auth) == {"Bearer tok"}
    with pytest.raises(sl.SeedError, match="read-only client"):
        sl.auth_for("oauth", URL, store, {"CLOUDOPS_SERVICENOW_CLIENT_ID": "seed-client"})


def test_basic_auth_refuses_the_admin_account() -> None:
    store = EnvFileSecretStore({"SERVICENOW_SEED_USER": "admin",
                                "SERVICENOW_SEED_PASSWORD": SECRET})
    with pytest.raises(sl.SeedError, match="admin"):
        sl.auth_for("basic", URL, store, {})


@pytest.mark.parametrize("url", ["https://getronics.service-now.com", "http://dev1234567.service-now.com",
                                 "https://dev1234567.service-now.com/api", "https://dev1.example.com"])
def test_only_a_personal_developer_instance_is_accepted(url: str) -> None:
    with pytest.raises(sl.SeedError, match="personal developer instance"):
        sl.check_instance(url)
    assert sl.check_instance(URL + "/") == URL


async def test_logs_hold_counts_only(capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeInstance()
    sl._print("seed", await sl.seed(_api(fake), SIM))
    out = capsys.readouterr().out
    assert SECRET not in out
    assert all(line.startswith("  seed ") for line in out.splitlines())
    assert not any(r["short_description"] in out for r in sl.records(SIM)["incident"])
