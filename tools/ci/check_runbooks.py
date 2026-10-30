"""CI gate: static validation of runbook manifests (LLD, Runbook manifest; ADR 0013).

Every ``runbooks/**/*.yaml`` must be a well-formed, closed manifest:

- ``apiVersion: cloudops/v1``, ``kind: Runbook``, and nothing the schema does not
  name (an unknown key is an error, never ignored);
- every parameter typed and bounded: a string needs an anchored ``pattern``
  (``^...$``), an integer a ``minimum`` and ``maximum``, an enum its ``values``;
- no interpolation syntax anywhere (``${``, ``$(``, ``{{``, backticks): scripts
  take parameters as native arguments, never spliced into a command;
- ``rollback`` present for action class C and above;
- at least one post-condition with a bounded timeout: a runbook that cannot prove
  its own success is not approved;
- no ``signature``: only the pipeline signs, on the protected branch, so a
  signature in source means someone signed by hand.

Standard library plus PyYAML (safe loading only). Usage:
python tools/ci/check_runbooks.py [repo_root]
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import yaml

CLASSES = ("A", "B", "C", "D", "E")
ROLLBACK_FROM = "C"
PLATFORMS = frozenset({"azure", "aws", "resolve"})
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
NAME = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,62}$")
INTERPOLATION = re.compile(r"\$\{|\$\(|\{\{|`|%\(")
MAX_TARGETS = 50
MAX_TIMEOUT = 3600

TOP_KEYS = {"apiVersion", "kind", "metadata", "spec"}
META_KEYS = {"id", "version", "actionClass", "platforms", "resourceTypes"}
SPEC_KEYS = {"parameters", "blastRadius", "preConditions", "steps", "rollback",
             "postConditions"}
PARAM_KEYS = {"name", "type", "pattern", "minimum", "maximum", "values", "required",
              "description"}


def _closed(obj: Mapping[str, Any], allowed: set[str], where: str) -> Iterator[str]:
    for key in sorted(set(obj) - allowed):
        yield f"{where}: unknown key '{key}'"


def _slugs(value: Any, where: str, *, required: bool) -> Iterator[str]:
    if value is None and not required:
        return
    if not isinstance(value, list) or (required and not value):
        yield f"{where}: must be a {'non-empty ' if required else ''}list of step ids"
        return
    for item in value:
        if not isinstance(item, str) or not SLUG.match(item):
            yield f"{where}: '{item}' is not a step id (lower-case, digits, hyphens)"


def _interpolation(value: Any, where: str) -> Iterator[str]:
    """Patterns are regexes and may use $ anchors; nothing else may interpolate."""
    if isinstance(value, Mapping):
        for k, v in value.items():
            if k == "pattern":
                continue
            yield from _interpolation(v, f"{where}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _interpolation(v, f"{where}[{i}]")
    elif isinstance(value, str) and INTERPOLATION.search(value):
        yield f"{where}: interpolation syntax is not allowed"


def _parameter(p: Any, where: str) -> Iterator[str]:
    if not isinstance(p, Mapping):
        yield f"{where}: must be a mapping"
        return
    yield from _closed(p, PARAM_KEYS, where)
    if not isinstance(p.get("name"), str) or not NAME.match(p["name"]):
        yield f"{where}: name must be an identifier"
    kind = p.get("type")
    if kind == "string":
        pattern = p.get("pattern")
        if not isinstance(pattern, str) or not (pattern.startswith("^") and pattern.endswith("$")):
            yield f"{where}: a string parameter needs an anchored pattern (^...$)"
        else:
            try:
                re.compile(pattern)
            except re.error:
                yield f"{where}: pattern is not a valid regular expression"
    elif kind == "integer":
        lo, hi = p.get("minimum"), p.get("maximum")
        if not (isinstance(lo, int) and isinstance(hi, int) and lo <= hi):
            yield f"{where}: an integer parameter needs minimum <= maximum"
    elif kind == "enum":
        values = p.get("values")
        if not (isinstance(values, list) and values and all(isinstance(v, str) for v in values)):
            yield f"{where}: an enum parameter needs a non-empty list of values"
    elif kind != "boolean":
        yield f"{where}: type must be string, integer, enum or boolean"
    if not isinstance(p.get("required"), bool):
        yield f"{where}: required must be true or false"


def validate(doc: Any) -> list[str]:
    if not isinstance(doc, Mapping):
        return ["the manifest must be a mapping"]
    errors: list[str] = []
    if "signature" in doc:
        errors.append("signature: only the pipeline signs a runbook (on the protected branch)")
    errors.extend(_closed({k: v for k, v in doc.items() if k != "signature"}, TOP_KEYS, "root"))
    if doc.get("apiVersion") != "cloudops/v1" or doc.get("kind") != "Runbook":
        errors.append("apiVersion must be cloudops/v1 and kind Runbook")
    meta = doc.get("metadata")
    spec = doc.get("spec")
    if not isinstance(meta, Mapping) or not isinstance(spec, Mapping):
        return [*errors, "metadata and spec are required mappings"]
    errors.extend(_closed(meta, META_KEYS, "metadata"))
    errors.extend(_closed(spec, SPEC_KEYS, "spec"))
    if not isinstance(meta.get("id"), str) or not SLUG.match(meta["id"]):
        errors.append("metadata.id must be a slug")
    version = meta.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        errors.append("metadata.version must be a positive integer")
    action_class = meta.get("actionClass")
    if action_class not in CLASSES:
        errors.append(f"metadata.actionClass must be one of {', '.join(CLASSES)}")
    platforms = meta.get("platforms")
    if not (isinstance(platforms, list) and platforms and set(platforms) <= PLATFORMS):
        errors.append(f"metadata.platforms must be a non-empty subset of {sorted(PLATFORMS)}")
    types = meta.get("resourceTypes")
    if not (isinstance(types, list) and types and all(isinstance(t, str) for t in types)):
        errors.append("metadata.resourceTypes must be a non-empty list")

    params = spec.get("parameters", [])
    if not isinstance(params, list):
        errors.append("spec.parameters must be a list")
    else:
        for i, p in enumerate(params):
            errors.extend(_parameter(p, f"spec.parameters[{i}]"))
        names = [p.get("name") for p in params if isinstance(p, Mapping)]
        if len(names) != len(set(names)):
            errors.append("spec.parameters: names must be unique")
    blast = spec.get("blastRadius")
    targets = blast.get("maxTargets") if isinstance(blast, Mapping) else None
    if not (isinstance(targets, int) and not isinstance(targets, bool)
            and 1 <= targets <= MAX_TARGETS):
        errors.append(f"spec.blastRadius.maxTargets must be 1..{MAX_TARGETS}")
    errors.extend(_slugs(spec.get("preConditions"), "spec.preConditions", required=False))
    errors.extend(_slugs(spec.get("steps"), "spec.steps", required=True))
    needs_rollback = action_class in CLASSES and action_class >= ROLLBACK_FROM
    errors.extend(_slugs(spec.get("rollback"), "spec.rollback", required=needs_rollback))
    posts = spec.get("postConditions")
    if not (isinstance(posts, list) and posts):
        errors.append("spec.postConditions: a runbook must prove its own success")
    else:
        for i, post in enumerate(posts):
            ok = (isinstance(post, Mapping) and set(post) <= {"id", "timeoutSeconds"}
                  and isinstance(post.get("id"), str) and SLUG.match(post["id"])
                  and isinstance(post.get("timeoutSeconds"), int)
                  and 1 <= post["timeoutSeconds"] <= MAX_TIMEOUT)
            if not ok:
                errors.append(f"spec.postConditions[{i}]: needs an id and "
                              f"timeoutSeconds 1..{MAX_TIMEOUT}")
    errors.extend(_interpolation(doc, "root"))
    return errors


def check(root: Path) -> list[str]:
    problems: list[str] = []
    for path in sorted((root / "runbooks").glob("**/*.yaml")):
        rel = path.relative_to(root).as_posix()
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            problems.append(f"{rel}: not valid YAML")
            continue
        problems.extend(f"{rel}: {e}" for e in validate(doc))
    return problems


def main(argv: list[str]) -> int:
    root = Path(argv[1] if len(argv) > 1 else ".").resolve()
    problems = check(root)
    for p in problems:
        print(p, file=sys.stderr)
    if problems:
        print(f"runbooks: {len(problems)} problem(s)", file=sys.stderr)
        return 1
    count = len(list((root / "runbooks").glob("**/*.yaml")))
    print(f"runbooks: {count} manifest(s) valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
