"""Set an image's tag in Kustomize overlays, keeping every comment (ADR 0013).

    python tools/ci/set_image_tag.py TAG IMAGE [IMAGE...] -- OVERLAY [OVERLAY...]

Rewrites the ``newTag`` that follows ``newName: <registry>/<image>`` in each
overlay's kustomization.yaml. The main pipeline uses it to point dev at the images
it just built; it is the only line it ever changes. An image an overlay does not
list is skipped there; an image no overlay lists is an error, never a silent no-op.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

TAG = re.compile(r"^[0-9a-f]{7,40}$")


def set_tags(text: str, images: list[str], tag: str) -> tuple[str, set[str]]:
    found: set[str] = set()
    for image in images:
        pattern = re.compile(
            rf"(newName:\s*\S+/{re.escape(image)}\s*\n\s*newTag:\s*)(\S+)", re.MULTILINE)
        text, n = pattern.subn(lambda m: m.group(1) + tag, text)
        if n:
            found.add(image)
    return text, found


def main(argv: list[str]) -> int:
    if "--" not in argv or len(argv) < 5:
        print(__doc__, file=sys.stderr)
        return 2
    split = argv.index("--")
    tag, images, overlays = argv[1], argv[2:split], argv[split + 1:]
    if not TAG.match(tag):
        print(f"error: '{tag}' is not a commit hash", file=sys.stderr)
        return 2
    seen: set[str] = set()
    for overlay in overlays:
        path = Path(overlay) / "kustomization.yaml"
        text, found = set_tags(path.read_text(encoding="utf-8"), images, tag)
        path.write_text(text, encoding="utf-8")
        seen |= found
        print(f"{path}: {', '.join(sorted(found)) or 'nothing'} -> {tag}")
    missing = sorted(set(images) - seen)
    if missing:
        print(f"error: no overlay lists {', '.join(missing)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
