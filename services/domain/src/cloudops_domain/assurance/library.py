"""A control library file, validated before anything is loaded.

    library = ControlLibrary.from_file(Path("controls.yaml"))
    await conn.scalar(LOAD, library.load_params(actor))

The library is reference data Security supplies (LLD, Pre-deployment
readiness: "98 control definitions with evidence sources"). None ships with
the platform. The file says which framework and version it is, and how many
controls it holds (``expected_count``), so a truncated file is refused rather
than half-loaded.

Evidence routing is declared per control: ``evidence_source`` names the system
that can answer it, and an ``automated`` control names the registered query
(``cloudops_domain.assurance.evidence.QUERIES``) that asks it. A query that
does not exist, or that asks a different system than the one declared, is a
validation error. A ``narrative`` control is answered from documents and
evidence, drafted by a model and approved by a person; a ``manual`` one is
answered by a person alone and never scores.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final, Literal
from uuid import UUID

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text

from cloudops_domain.assurance.evidence import QUERIES

Assessment = Literal["automated", "narrative", "manual"]
EvidenceSource = Literal["azure", "aws", "servicenow", "qualys", "corestack", "entra",
                         "sharepoint", "policy_library", "human"]

LOAD: Final = text(
    "SELECT access.load_controls(:actor, :framework, :version, CAST(:controls AS jsonb))"
)


class LibraryError(ValueError):
    pass


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ControlDefinition(_Closed):
    code: str = Field(pattern=r"^[A-Z0-9][A-Z0-9._-]{1,39}$")
    domain: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=300)
    requirement: str = Field(min_length=1, max_length=4000)
    assessment: Assessment
    evidence_source: EvidenceSource
    evidence_query: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _routing(self) -> ControlDefinition:
        if self.assessment == "automated":
            query = QUERIES.get(self.evidence_query or "")
            if query is None:
                raise ValueError(f"{self.code}: unknown evidence_query {self.evidence_query!r}")
            if query.source_system != self.evidence_source:
                raise ValueError(f"{self.code}: {query.name} asks {query.source_system}, "
                                 f"not {self.evidence_source}")
            query.check_parameters(self.parameters, self.code)
        elif self.evidence_query is not None:
            raise ValueError(f"{self.code}: only an automated control names an evidence_query")
        if self.assessment == "manual" and self.evidence_source != "human":
            raise ValueError(f"{self.code}: a manual control's evidence source is human")
        return self


class ControlLibrary(_Closed):
    framework: str = Field(pattern=r"^[a-z0-9_]{1,40}$")
    version: str = Field(min_length=1, max_length=40)
    expected_count: int = Field(ge=1)
    controls: list[ControlDefinition] = Field(min_length=1)

    @model_validator(mode="after")
    def _complete(self) -> ControlLibrary:
        codes = [c.code for c in self.controls]
        duplicates = sorted({c for c in codes if codes.count(c) > 1})
        if duplicates:
            raise ValueError(f"duplicate control codes: {', '.join(duplicates)}")
        if len(codes) != self.expected_count:
            raise ValueError(f"expected {self.expected_count} controls, found {len(codes)}")
        return self

    @classmethod
    def from_file(cls, path: Path) -> ControlLibrary:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw) if path.suffix == ".json" else yaml.safe_load(raw)
        if not isinstance(data, dict):
            raise LibraryError("a control library is a mapping with framework, version, controls")
        try:
            return cls.model_validate(data)
        except ValueError as exc:
            raise LibraryError(str(exc)) from None

    def load_params(self, actor: UUID) -> dict[str, Any]:
        return {
            "actor": actor, "framework": self.framework, "version": self.version,
            "controls": json.dumps([c.model_dump(mode="json") for c in self.controls]),
        }
