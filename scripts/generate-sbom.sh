#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
output_input=${1:-}
if [[ -z "$output_input" || "$output_input" != /* ]]; then
  echo "usage: $0 /absolute/path/to/new-sbom-directory" >&2
  exit 2
fi
output=${output_input%/}
if [[ -e "$output" ]]; then
  echo "SBOM output directory already exists; refusing to overwrite: $output" >&2
  exit 2
fi
if [[ "$output" == "/" || "$output" == "$repo_root" ]]; then
  echo "unsafe SBOM output target" >&2
  exit 2
fi
umask 077
mkdir -p "$output"

mapfile -t images < <(docker compose -f "$repo_root/compose.yaml" --profile '*' config --images | sort -u)
for image in "${images[@]}"; do
  if ! docker image inspect "$image" >/dev/null 2>&1; then
    docker pull "$image" >/dev/null
  fi
  filename=$(printf '%s' "$image" | tr '/:@' '____')
  docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
    anchore/syft:v1.29.0 "docker:$image" -o cyclonedx-json > "$output/$filename.cdx.json"
done
(
  cd "$output"
  sha256sum ./*.cdx.json > MANIFEST.sha256
)
chmod -R go-rwx "$output"
echo "$output"
