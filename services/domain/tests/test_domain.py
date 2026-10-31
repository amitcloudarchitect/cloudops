"""Domain logic without a database: money presentation, scoring, document kinds
and staleness rules, library validation, evidence thresholds, the signed pack,
the PDF writer and the acceptance diff."""

from __future__ import annotations

import base64
import copy
import hashlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cloudops_domain import knowledge
from cloudops_domain.assurance.evidence import QUERIES
from cloudops_domain.assurance.library import ControlLibrary, LibraryError
from cloudops_domain.assurance.pack import (
    Ed25519Signer,
    SigningUnavailableError,
    UnavailableSigner,
    build_manifest,
    manifest_bytes,
    verify,
)
from cloudops_domain.assurance.scoring import ScoredResponse, score
from cloudops_domain.reporting.acceptance import compare
from cloudops_domain.reporting.money import present, round_money
from cloudops_domain.reporting.pdf import render_pdf
from cloudops_domain.reporting.render import DRAFT_BANNER, render_lines, text_lines
from cloudops_domain.reporting.sections import (
    Figure,
    PackContent,
    Section,
    Table,
    month_bounds,
    narrative_input,
)
from cloudops_tenancy.telemetry import TelemetryError

# --- money -------------------------------------------------------------------------------


@pytest.mark.parametrize(("amount", "currency", "shown"), [
    (Decimal("1234.565"), "EUR", "EUR 1,234.57"),  # half-up, once
    (Decimal("1234.564999"), "GBP", "GBP 1,234.56"),
    (Decimal("-0.004"), "USD", "USD 0.00"),  # no negative zero
    (Decimal("-1500.5"), "USD", "USD -1,500.50"),
    (Decimal("1234567.5"), "JPY", "JPY 1,234,568"),  # no minor units
    (Decimal("12.3455"), "BHD", "BHD 12.346"),  # three minor units
])
def test_money_is_rounded_once_in_its_own_currency(amount: Decimal, currency: str,
                                                   shown: str) -> None:
    assert present(amount, currency) == shown


def test_money_is_never_a_float() -> None:
    with pytest.raises(TypeError):
        round_money(1.5, "EUR")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="ISO 4217"):
        present(Decimal(1), "euro")


# --- scoring -----------------------------------------------------------------------------


def test_score_is_100_50_0_with_manual_and_unapproved_excluded() -> None:
    s = score([
        ScoredResponse("A", "compliant", True),
        ScoredResponse("B", "partially_compliant", True),
        ScoredResponse("C", "non_compliant", True),
        ScoredResponse("D", "manual", True),
        ScoredResponse("E", "not_applicable", True),
        ScoredResponse("F", None, False),  # drafted, never approved: pending, not scored
    ])
    assert s.percent == Decimal("50.00")
    assert (s.scorable, s.pending, s.excluded, s.points) == (3, 1, 2, 150)


def test_an_unapproved_compliant_never_scores() -> None:
    s = score([ScoredResponse("A", "compliant", False), ScoredResponse("B", "non_compliant", True)])
    assert s.percent == Decimal("0.00")
    assert s.pending == 1


def test_nothing_scorable_is_no_score_not_zero() -> None:
    assert score([ScoredResponse("A", "manual", True)]).percent is None
    assert score([]).percent is None


def test_score_rounds_once_half_up() -> None:
    s = score([ScoredResponse("A", "compliant", True), ScoredResponse("B", "compliant", True),
               ScoredResponse("C", "partially_compliant", True)])
    assert s.percent == Decimal("83.33")


# --- knowledge ---------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "folder", "kind"), [
    ("NWL Solution Design v3.md", None, "design"),
    ("hld.txt", None, "design"),
    ("Service Transition Pack.md", None, "handover"),
    ("handover-and-design-notes.md", None, "handover"),  # handover wins
    ("restart-print-spooler.md", "/Runbooks", "runbook"),
    ("patching-standard.md", "/Runbooks", "policy"),  # the name decides first
    ("contacts.txt", "/Handover", "handover"),  # then the folder
    ("contacts.txt", "/Misc", "other"),
])
def test_document_kind_comes_from_name_then_folder(name: str, folder: str | None,
                                                   kind: str) -> None:
    assert knowledge.classify_document(name, folder) == kind


def test_document_text_cannot_change_its_kind() -> None:
    # Only the name and folder are read: there is no parameter for the text.
    assert knowledge.classify_document("notes.md") == "other"


def test_staleness_rules() -> None:
    now = datetime(2026, 10, 1, tzinfo=UTC)
    recent = now - timedelta(days=30)
    assert knowledge.staleness_reasons(as_of=recent, now=now, removed=[], added_since=1,
                                       live=10) == ()
    assert knowledge.staleness_reasons(as_of=recent, now=now, removed=["vm-01"],
                                       added_since=0, live=10) == ("removed_resources",)
    assert knowledge.staleness_reasons(as_of=recent, now=now, removed=[], added_since=2,
                                       live=10) == ("estate_changed",)
    assert knowledge.staleness_reasons(as_of=now - timedelta(days=400), now=now, removed=[],
                                       added_since=0, live=0) == ("not_updated",)
    assert knowledge.staleness_reasons(as_of=None, now=now, removed=[], added_since=0,
                                       live=0) == ("not_updated",)


def test_coverage_takes_the_best_document_per_kind() -> None:
    t1, t2 = uuid4(), uuid4()

    def status(tenant: Any, kind: str, reasons: tuple[str, ...]) -> knowledge.DocumentStatus:
        return knowledge.DocumentStatus(uuid4(), tenant, "x", "d", kind,  # type: ignore[arg-type]
                                        None, None, 0, (), 0, 0, reasons)

    gaps = knowledge.coverage(
        [(t1, "one"), (t2, "two")],
        [status(t1, "design", ("not_updated",)), status(t1, "design", ()),
         status(t1, "handover", ("removed_resources",)), status(t2, "runbook", ())])
    assert gaps[0].coverage == {"design": "current", "handover": "stale"}
    assert gaps[1].coverage == {"design": "missing", "handover": "missing"}
    assert gaps[0].has_gap
    assert gaps[1].has_gap


def test_page_follows_form_feeds() -> None:
    body = "page one\fpage two\fpage three"
    assert knowledge.page_at(body, 0) == 1
    assert knowledge.page_at(body, body.index("two")) == 2
    assert knowledge.page_at(body, len(body)) == 3


# --- the control library -------------------------------------------------------------------


def _library(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "framework": "test_framework", "version": "2026.1", "expected_count": 3,
        "controls": [
            {"code": "TEST-VM-01", "domain": "Vulnerability", "title": "Patch criticals",
             "requirement": "Critical vulnerabilities are remediated within 30 days.",
             "assessment": "automated", "evidence_source": "qualys",
             "evidence_query": "qualys.open_vulnerabilities_over_age",
             "parameters": {"min_severity": 4, "max_age_days": 30, "compliant_max": 0,
                            "partial_max": 5}},
            {"code": "TEST-GOV-01", "domain": "Governance", "title": "Change policy",
             "requirement": "A documented change policy exists.", "assessment": "narrative",
             "evidence_source": "policy_library"},
            {"code": "TEST-PHY-01", "domain": "Physical", "title": "Badge access",
             "requirement": "Data centre access is badge controlled.", "assessment": "manual",
             "evidence_source": "human"},
        ],
    }
    data.update(overrides)
    return data


def test_a_valid_library_loads() -> None:
    lib = ControlLibrary.model_validate(_library())
    params = lib.load_params(uuid4())
    assert params["framework"] == "test_framework"
    assert '"TEST-VM-01"' in params["controls"]


@pytest.mark.parametrize(("change", "message"), [
    ({"expected_count": 98}, "expected 98 controls, found 3"),
    ({"controls": "dup"}, "duplicate control codes"),
    ({"controls": "unknown_query"}, "unknown evidence_query"),
    ({"controls": "wrong_source"}, "asks qualys, not entra"),
    ({"controls": "missing_threshold"}, "needs threshold 'compliant_max'"),
    ({"controls": "manual_not_human"}, "manual control's evidence source is human"),
    ({"controls": "narrative_query"}, "only an automated control names an evidence_query"),
])
def test_an_invalid_library_is_refused_whole(change: dict[str, Any], message: str) -> None:
    data = _library()
    controls = data["controls"]
    match change.get("controls"):
        case "dup":
            controls.append(copy.deepcopy(controls[0]))
            data["expected_count"] = 4
        case "unknown_query":
            controls[0]["evidence_query"] = "qualys.everything_is_fine"
        case "wrong_source":
            controls[0]["evidence_source"] = "entra"
        case "missing_threshold":
            del controls[0]["parameters"]["compliant_max"]
        case "manual_not_human":
            controls[2]["evidence_source"] = "sharepoint"
        case "narrative_query":
            controls[1]["evidence_query"] = "entra.role_holders"
        case None:
            data.update(change)
    with pytest.raises(ValueError, match=message):
        ControlLibrary.model_validate(data)


def test_library_file_round_trip(tmp_path: Path) -> None:
    import yaml

    path = tmp_path / "library.yaml"
    path.write_text(yaml.safe_dump(_library()), encoding="utf-8")
    assert len(ControlLibrary.from_file(path).controls) == 3
    path.write_text("- not a mapping", encoding="utf-8")
    with pytest.raises(LibraryError):
        ControlLibrary.from_file(path)


# --- evidence thresholds --------------------------------------------------------------------


@pytest.mark.parametrize(("measure", "proposed"), [
    (Decimal(0), "compliant"), (Decimal(5), "partially_compliant"), (Decimal(6), "non_compliant"),
    (None, None),
])
def test_at_most_thresholds(measure: Decimal | None, proposed: str | None) -> None:
    q = QUERIES["qualys.open_vulnerabilities_over_age"]
    got, basis = q.evaluate(measure, {"compliant_max": 0, "partial_max": 5})
    assert got == proposed
    assert basis


@pytest.mark.parametrize(("measure", "proposed"), [
    (Decimal("0.95"), "compliant"), (Decimal("0.90"), "partially_compliant"),
    (Decimal("0.5"), "non_compliant"),
])
def test_at_least_thresholds(measure: Decimal, proposed: str) -> None:
    q = QUERIES["servicenow.incident_sla_attainment"]
    assert q.evaluate(measure, {"compliant_min": "0.95", "partial_min": "0.9"})[0] == proposed


def test_every_query_binds_its_tenant() -> None:
    for q in QUERIES.values():
        assert ":tenant" in q.sql, q.name


# --- the signed pack ----------------------------------------------------------------------


def _manifest() -> dict[str, Any]:
    return build_manifest(
        assessment={"id": "a", "framework": "f"},
        responses=[{"control_code": "C", "result": "compliant"}],
        evidence=[{"id": "e", "raw_value": {"holders": 2}}],
        score=score([ScoredResponse("C", "compliant", True)]),
        exported_at=datetime(2026, 10, 31, tzinfo=UTC), exported_by=uuid4())


def test_a_signed_pack_verifies_and_a_tampered_one_does_not() -> None:
    signer = Ed25519Signer(Ed25519PrivateKey.generate())
    doc = signer.sign(_manifest()).as_document()
    assert verify(doc, signer.public_key)
    tampered = copy.deepcopy(doc)
    tampered["manifest"]["responses"][0]["result"] = "non_compliant"
    assert not verify(tampered, signer.public_key)
    forged = copy.deepcopy(tampered)
    forged["manifest_sha256"] = hashlib.sha256(manifest_bytes(forged["manifest"])).hexdigest()
    assert not verify(forged, signer.public_key)  # a recomputed digest needs the key too
    other = Ed25519Signer(Ed25519PrivateKey.generate())
    assert not verify(doc, other.public_key)


def test_the_seed_signer_and_the_unavailable_signer() -> None:
    seed = base64.b64encode(bytes(range(32))).decode()
    a, b = Ed25519Signer.from_seed(seed), Ed25519Signer.from_seed(seed)
    assert a.key_id == b.key_id
    with pytest.raises(ValueError, match="32 bytes"):
        Ed25519Signer.from_seed(base64.b64encode(b"short").decode())
    with pytest.raises(SigningUnavailableError):
        UnavailableSigner().sign(_manifest())


# --- rendering, PDF and the acceptance diff ------------------------------------------------


def _content() -> PackContent:
    return PackContent(
        uuid4(), "Alpha Ltd", date(2026, 9, 1), "EUR", datetime(2026, 10, 1, 6, 0, tzinfo=UTC),
        (
            Section("executive_summary", "Executive summary", True, None, (
                Figure("cloud_cost", "Cloud cost", "1234.565", "money",
                       "EUR", "cloud_billing", "q"),
                Figure("incident_sla_attainment", "Incidents resolved within SLA",
                       "66.666666", "percent", "percent", "servicenow", "q"),
            )),
            Section("volumetrics", "Volumetrics", True, None, (
                Figure("incidents_opened", "Incidents opened", "1204", "count", "incidents",
                       "servicenow", "q"),), (
                Table("by_kind", "Work by type", ("Type", "Opened"), ("text", "count"),
                      (("incident", "1204"),)),)),
            Section("csat", "Customer satisfaction", False, "no survey source is connected"),
        ),
    )


def test_render_prints_people_not_drafts_and_marks_unreleased_packs() -> None:
    lines = text_lines(render_lines(_content(), {"volumetrics": "A quiet month."},
                                    released=False, version=2))
    assert DRAFT_BANNER in lines
    assert "Cloud cost: EUR 1,234.57" in lines
    assert "Incidents resolved within SLA: 66.67%" in lines
    assert "Incidents opened: 1,204" in lines
    assert "A quiet month." in lines
    assert "Not captured: no survey source is connected." in lines
    released = text_lines(render_lines(_content(), {}, released=True, version=2))
    assert DRAFT_BANNER not in released


def test_table_money_prints_in_the_pack_currency_and_hours_round_once() -> None:
    usd = PackContent(
        uuid4(), "Charlie Inc", date(2026, 9, 1), "USD", datetime(2026, 10, 1, tzinfo=UTC),
        (Section("financial_dashboard", "Financial dashboard", True, None, (
            Figure("effort_hours", "Observed effort", "120.2500", "hours", "hours",
                   "servicenow", "q"),), (
            Table("effort_variance", "Effort by workgroup", ("Workgroup", "Hours", "Excess"),
                  ("text", "hours", "money"), (("Cloud Ops", "90.25", "-1234.565"),)),)),))
    lines = text_lines(render_lines(usd, {}, released=True, version=1))
    assert "Observed effort: 120.3 hours" in lines
    assert "Cloud Ops | 90.3 hours | USD -1,234.57" in lines


def test_snapshot_round_trips() -> None:
    content = _content()
    again = PackContent.from_json(content.tenant_id, content.to_json())
    assert again == content


def test_narrative_input_is_figures_and_tables_only() -> None:
    data = narrative_input(_content().section("volumetrics"))
    assert set(data) == {"section", "title", "figures", "tables"}


def test_the_pdf_is_valid_deterministic_and_prints_the_euro_sign() -> None:
    lines = render_lines(_content(), {}, released=True, version=1)
    pdf = render_pdf(lines, "Alpha 2026-09")
    assert pdf.startswith(b"%PDF-1.4")
    assert pdf.rstrip().endswith(b"%%EOF")
    assert render_pdf(lines, "Alpha 2026-09") == pdf
    assert b"EUR 1,234.57" in pdf
    many = render_lines(_content(), {"volumetrics": "\n\n".join(["Para."] * 200)},
                        released=True, version=1)
    assert render_pdf(many, "x").count(b"/Type /Page ") > 1
    euro = render_pdf(render_lines(_content(), {"volumetrics": "Saved €5."},
                                   released=True, version=1), "x")
    assert b"Saved \x805." in euro  # WinAnsi's euro sign


def test_acceptance_matches_on_whitespace_and_flags_currency_presentation() -> None:
    generated = ["Alpha  Ltd", "Cloud cost: EUR 1,234.57", "Incidents opened: 1,204"]
    same = ["  Alpha Ltd ", "", "Cloud cost: EUR 1,234.57", "Incidents opened: 1,204"]
    assert compare(generated, same).matches
    manual = ["Alpha Ltd", "Cloud cost: €1,234.57", "Incidents opened: 1,204"]
    result = compare(generated, manual)
    assert not result.matches
    assert result.currency_mismatches == [("Cloud cost: €1,234.57",
                                           "Cloud cost: EUR 1,234.57")]
    assert any(line.startswith("-Cloud cost") for line in result.diff)


def test_month_bounds() -> None:
    start, end = month_bounds(date(2026, 12, 1))
    assert (start.date(), end.date()) == (date(2026, 12, 1), date(2027, 1, 1))
    with pytest.raises(ValueError, match="first day"):
        month_bounds(date(2026, 12, 2))


async def test_a_truncated_availability_read_is_a_gap_not_a_failed_pack() -> None:
    from typing import cast

    from cloudops_domain.reporting import sections
    from cloudops_domain.reporting.availability import AvailabilityTruncatedError
    from cloudops_tenancy import Role, ScopedConnection, TenantScope

    class Truncated:
        async def availability(self, scope: TenantScope, start: datetime, end: datetime
                               ) -> dict[UUID, float]:
            raise AvailabilityTruncatedError("more hourly measurements than one read returns")

    tenant = uuid4()
    got = await sections.availability(
        cast(ScopedConnection, None), TenantScope([tenant], Role.ENGINEER, "test"), tenant,
        date(2026, 9, 1), Truncated())
    assert (got.captured, got.gap) == (False, "more hourly measurements than one read returns")


@pytest.mark.parametrize("failure", [httpx.ConnectError("refused"),
                                     TelemetryError("telemetry store refused the request")])
async def test_an_unreachable_telemetry_store_is_a_gap_and_a_scope_defect_is_not(
    failure: Exception,
) -> None:
    from typing import cast

    from cloudops_domain.reporting.availability import (
        AvailabilityUnreachableError,
        TelemetryAvailability,
    )
    from cloudops_tenancy import Role, TenantScope
    from cloudops_tenancy.telemetry import TelemetryScopeError, TelemetryStore

    class Store:
        def __init__(self, exc: Exception) -> None:
            self.exc = exc

        async def query(self, *_: object) -> list[object]:
            raise self.exc

    scope = TenantScope([uuid4()], Role.ENGINEER, "test")
    start = datetime(2026, 9, 1, tzinfo=UTC)
    with pytest.raises(AvailabilityUnreachableError):
        await TelemetryAvailability(cast(TelemetryStore, Store(failure))).availability(
            scope, start, start + timedelta(days=30))
    with pytest.raises(TelemetryScopeError):
        await TelemetryAvailability(cast(TelemetryStore, Store(
            TelemetryScopeError("outside the scope")))).availability(
            scope, start, start + timedelta(days=30))
