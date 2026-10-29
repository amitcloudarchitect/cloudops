"""Container conventions (step 11; LLD, Compute pod controls). Each test fails if a
rule the images rely on is quietly dropped."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = REPO_ROOT / "infra/docker/python.Dockerfile"
BAKE = REPO_ROOT / "docker-bake.hcl"
GRYPE = REPO_ROOT / ".grype.yaml"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"


def _froms() -> list[tuple[str, str | None]]:
    """(image, stage name) for every FROM."""
    found = []
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        m = re.match(r"FROM\s+(\S+)(?:\s+AS\s+(\S+))?", line)
        if m:
            found.append((m.group(1), m.group(2)))
    return found


def test_every_external_base_is_pinned_by_digest() -> None:
    stages = {name for _, name in _froms() if name}
    external = [image for image, _ in _froms() if image not in stages]
    assert external
    for image in external:
        assert re.search(r"@sha256:[0-9a-f]{64}$", image), image


def test_the_runtime_is_distroless_and_never_root() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    froms = dict((name, image) for image, name in _froms())
    assert froms["runtime"] == "distroless"
    assert froms["distroless"].startswith("gcr.io/distroless/")
    runtime = text[text.index("FROM distroless AS runtime"):]
    users = re.findall(r"^USER\s+(\S+)", runtime, re.M)
    assert users == ["65532:65532"]
    assert "pip" in text  # pip is removed from the interpreter


def test_bake_builds_exactly_the_final_stages_each_with_its_package() -> None:
    finals = {name for image, name in _froms() if image == "runtime"}
    bake = BAKE.read_text(encoding="utf-8")
    targets = set(re.findall(r'^target "([a-z][a-z0-9-]*)"', bake, re.M))
    assert targets == finals
    default = re.search(r'group "default" \{\s*targets = \[([^\]]*)\]', bake)
    assert default
    assert set(re.findall(r'"([^"]+)"', default.group(1))) == finals
    for target in finals:
        block = bake[bake.index(f'target "{target}"'):].split("\n}\n", 1)[0]
        assert re.search(r'PACKAGE = "(cloudops-[a-z-]+|all)"', block), target


def test_dockerignore_is_an_allowlist_that_keeps_tests_out() -> None:
    lines = [ln.strip() for ln in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.startswith("#")]
    assert lines[0] == "*"
    assert "**/tests/" in lines
    allowed = [ln for ln in lines if ln.startswith("!")]
    for pattern in allowed:
        assert not re.search(r"\.env|tfvars|keypair|\.git|docs|evals|infra|tests", pattern), pattern


def test_vulnerability_exceptions_are_narrow() -> None:
    """Every exception names one CVE and one anchored package, is limited by fix
    state, and says why. A blanket rule would silently disable the gate."""
    text = GRYPE.read_text(encoding="utf-8")
    rules = text.split("\n  - vulnerability: ")[1:]
    assert rules
    for rule in rules:
        assert re.match(r"CVE-\d{4}-\d+\n", rule), rule
        assert re.search(r"\n    fix-state: (wont-fix|not-fixed)\n", rule), rule
        name = re.search(r"package: \{ name: '\^(.+)\$', type: deb \}", rule)
        assert name, rule
        assert "+" not in name.group(1).replace("\\+", ""), rule
        assert re.search(r'reason: "distroless base; .+; reviewed \d{4}-\d{2}-\d{2}"', rule), rule
