"""The tag bump the main pipeline commits: one line per image, comments kept."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[3]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "set_image_tag", REPO_ROOT / "tools/ci/set_image_tag.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tool = _load()
OVERLAY = """# a comment the pipeline must keep
images:
  - name: api
    newName: crcloudopsdevweu.azurecr.io/api
    newTag: 17baeb4
  - name: migrate
    newName: crcloudopsdevweu.azurecr.io/migrate
    newTag: c5867d0  # trailing comment
"""


def test_only_the_named_images_change() -> None:
    text, found = tool.set_tags(OVERLAY, ["api"], "abcdef1")
    assert found == {"api"}
    assert "newTag: abcdef1" in text
    assert "newTag: c5867d0  # trailing comment" in text
    assert text.startswith("# a comment the pipeline must keep")


def test_an_image_name_is_not_a_prefix_match() -> None:
    text, found = tool.set_tags(OVERLAY.replace("/api\n", "/api-gateway\n"), ["api"], "abcdef1")
    assert found == set()
    assert "abcdef1" not in text


def test_every_real_overlay_image_is_found() -> None:
    overlays = [REPO_ROOT / "infra/gitops/overlays/dev",
                REPO_ROOT / "infra/gitops/overlays/dev/workers"]
    seen: set[str] = set()
    for overlay in overlays:
        _, found = tool.set_tags((overlay / "kustomization.yaml").read_text(encoding="utf-8"),
                                 ["api", "migrate", "connector-servicenow", "worker-ai"], "abcdef1")
        seen |= found
    assert seen == {"api", "migrate", "connector-servicenow", "worker-ai"}
