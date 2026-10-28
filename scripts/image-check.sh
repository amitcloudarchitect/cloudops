#!/usr/bin/env bash
# The image gate: for each image, write an SBOM and fail on any critical or high
# vulnerability, except the reviewed base-image exceptions in .grype.yaml. Also
# refuses an image that would run as root.
#
#   scripts/image-check.sh cloudops/api:dev [more images...]
#
# SBOMs land in build/sbom/ (gitignored) as SPDX and CycloneDX JSON. Signing is not
# done here: it happens in the CI pipeline (step 13), with the pipeline's identity.
set -euo pipefail

for tool in docker syft grype; do
  command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 1; }
done
[[ $# -gt 0 ]] || { echo "usage: $0 IMAGE [IMAGE...]" >&2; exit 2; }

out=build/sbom
mkdir -p "$out"
failed=0
for image in "$@"; do
  name="${image##*/}"; name="${name//:/-}"
  echo "==> $image"
  user="$(docker image inspect -f '{{.Config.User}}' "$image")"
  if [[ -z "$user" || "$user" == "0" || "$user" == root* || "$user" == 0:* ]]; then
    echo "    runs as root (USER '$user')" >&2; failed=1; continue
  fi
  syft -q "docker:$image" -o "spdx-json=$out/$name.spdx.json" -o "cyclonedx-json=$out/$name.cdx.json"
  count="$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["packages"]))' "$out/$name.spdx.json")"
  echo "    sbom: $out/$name.spdx.json ($count packages)"
  if ! grype -q -c .grype.yaml "sbom:$out/$name.spdx.json" --fail-on high -o table; then
    echo "    FAILED: critical or high vulnerabilities" >&2; failed=1
  else
    echo "    scan: no critical or high"
  fi
done
exit "$failed"
