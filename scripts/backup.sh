#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
compose_file="$repo_root/compose.yaml"
target_input=${1:-}
if [[ -z "$target_input" ]]; then
  echo "usage: $0 /absolute/path/to/new-backup-directory" >&2
  exit 2
fi
if [[ "$target_input" != /* ]]; then
  echo "backup target must be an absolute path" >&2
  exit 2
fi
target=${target_input%/}
if [[ -e "$target" ]]; then
  echo "backup target already exists; refusing to overwrite: $target" >&2
  exit 2
fi
if [[ "$target" == "/" || "$target" == "$repo_root" || "$target" == "$repo_root/"* ]]; then
  echo "backup target must be outside the repository and cannot be root" >&2
  exit 2
fi

umask 077
mkdir -p "$target/object-storage"
started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)

docker compose -f "$compose_file" exec -T postgres \
  pg_dump --username youtuber --dbname youtuber --format custom --compress 9 \
  --no-owner --no-privileges > "$target/postgres.dump"

docker compose -f "$compose_file" --profile operations run --rm --no-deps \
  -v "$target/object-storage:/backup" backup-tool -c '
    set -eu
    export MC_CONFIG_DIR=/tmp/.mc
    access=$(cat /run/secrets/minio_access_key)
    secret=$(cat /run/secrets/minio_secret_key)
    mc alias set local http://minio:9000 "$access" "$secret" >/dev/null
    mc --quiet mirror --overwrite local/production-artifacts /backup/production-artifacts
  '

finished_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
database_sha=$(sha256sum "$target/postgres.dump" | awk '{print $1}')
object_count=$(find "$target/object-storage/production-artifacts" -type f 2>/dev/null | wc -l | tr -d ' ')
cat > "$target/backup.json" <<EOF
{
  "schema_version": "1.0",
  "started_at": "$started_at",
  "finished_at": "$finished_at",
  "database": {"file": "postgres.dump", "sha256": "$database_sha"},
  "object_storage": {"bucket": "production-artifacts", "file_count": $object_count},
  "consistency_model": "PostgreSQL MVCC snapshot plus immutable object copy; committed rows only reference objects written before commit"
}
EOF

(
  cd "$target"
  find . -type f ! -name MANIFEST.sha256 -print0 | sort -z | xargs -0 sha256sum > MANIFEST.sha256
)
chmod -R go-rwx "$target"
echo "$target"
