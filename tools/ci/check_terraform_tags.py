"""CI gate: every taggable resource in a Terraform plan carries the mandatory tags.

The tags module makes the mandatory set impossible to omit when it is used;
this makes it impossible not to use it. Run against a plan rendered as JSON:

    terraform plan -out=tfplan ... && terraform show -json tfplan > plan.json
    python tools/ci/check_terraform_tags.py plan.json

A resource is taggable when its planned state has a ``tags`` attribute, except
the ``azapi_resource`` types in ``UNTAGGABLE_AZAPI``, whose ARM type refuses tags
though azapi always plans the attribute (named exactly; any other azapi type is
checked). Fails
(exit 1) on any created or updated resource whose tags lack a mandatory key,
carry an empty value, or claim a managedby other than terraform. Also fails
on NSG rules listing "*" inside a port list, which plans cleanly but Azure
rejects at apply time.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

MANDATORY = ("owner", "environment", "costcentre", "application", "dataclass", "managedby")
CHECKED_ACTIONS = {"create", "update"}
# ARM types that take no tags at all (azapi's schema validation refuses them).
UNTAGGABLE_AZAPI = frozenset({
    "Microsoft.CostManagement/exports",  # the platform cost export (ADR 0023)
})


def _untaggable(rc: dict[str, Any], after: dict[str, Any]) -> bool:
    if rc.get("type") != "azapi_resource":
        return False
    return str(after.get("type", "")).split("@", 1)[0] in UNTAGGABLE_AZAPI


def violations(plan: dict[str, Any]) -> list[str]:
    # A failed plan still renders as JSON, with no resource changes. Passing
    # it would report "ok" on nothing.
    if plan.get("errored") is not False or "resource_changes" not in plan:
        return ["plan is errored or incomplete; nothing was checked"]
    out: list[str] = []
    for rc in plan.get("resource_changes", []):
        change = rc.get("change", {})
        if not CHECKED_ACTIONS & set(change.get("actions", [])):
            continue
        after = change.get("after") or {}
        if "tags" not in after or _untaggable(rc, after):
            continue  # not a taggable resource type
        for rule in after.get("security_rule") or []:
            if "*" in (rule.get("destination_port_ranges") or []) or "*" in (
                rule.get("source_port_ranges") or []
            ):
                out.append(
                    f"{rc.get('address', '?')}: rule {rule.get('name')} lists '*' in a port"
                    " range list; Azure rejects it, use the single-value port_range"
                )
        tags = after.get("tags") or {}
        address = rc.get("address", "?")
        missing = [k for k in MANDATORY if not str(tags.get(k) or "").strip()]
        if missing:
            out.append(f"{address}: missing mandatory tags {', '.join(missing)}")
        elif tags["managedby"] != "terraform":
            out.append(f"{address}: managedby must be terraform")
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_terraform_tags.py plan.json", file=sys.stderr)
        return 2
    try:
        plan = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"cannot read plan JSON ({type(exc).__name__}); nothing was checked", file=sys.stderr)
        return 1
    found = violations(plan)
    for v in found:
        print(v)
    if found:
        print(f"\n{len(found)} problem(s) with mandatory tags (LLD, Tagging).", file=sys.stderr)
        return 1
    print("mandatory tags: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
