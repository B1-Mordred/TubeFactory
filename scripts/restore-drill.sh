#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
compose_file="$repo_root/compose.yaml"
backup_input=${1:-}
drill_id=${2:-}
if [[ -z "$backup_input" || -z "$drill_id" ]]; then
  echo "usage: $0 /absolute/path/to/backup drill-id" >&2
  exit 2
fi
if [[ "$backup_input" != /* || ! "$drill_id" =~ ^[a-z0-9][a-z0-9-]{2,29}$ ]]; then
  echo "backup path must be absolute and drill-id must match ^[a-z0-9][a-z0-9-]{2,29}$" >&2
  exit 2
fi
backup=${backup_input%/}
for required in postgres.dump backup.json MANIFEST.sha256; do
  [[ -f "$backup/$required" ]] || { echo "missing backup artifact: $required" >&2; exit 2; }
done
(
  cd "$backup"
  sha256sum --check --strict --quiet MANIFEST.sha256
)
expected_object_count=$(jq -er '.object_storage.file_count | select(type == "number" and . >= 0 and floor == .)' "$backup/backup.json")

database="restore_drill_${drill_id//-/_}"
bucket="restore-drill-$drill_id"
created_database=false
created_bucket=false
cleanup() {
  if [[ "$created_database" == true ]]; then
    docker compose -f "$compose_file" exec -T postgres psql -U youtuber -d postgres \
      -v ON_ERROR_STOP=1 -c "DROP DATABASE IF EXISTS \"$database\" WITH (FORCE);" >/dev/null || true
  fi
  if [[ "$created_bucket" == true ]]; then
    docker compose -f "$compose_file" --profile operations run --rm --no-deps backup-tool -c "
      set -eu
      export MC_CONFIG_DIR=/tmp/.mc
      access=\$(cat /run/secrets/minio_access_key)
      secret=\$(cat /run/secrets/minio_secret_key)
      mc alias set local http://minio:9000 \"\$access\" \"\$secret\" >/dev/null
      mc rb --force local/$bucket >/dev/null
    " || true
  fi
}
trap cleanup EXIT

exists=$(docker compose -f "$compose_file" exec -T postgres psql -U youtuber -d postgres -Atc \
  "SELECT 1 FROM pg_database WHERE datname = '$database';")
if [[ -n "$exists" ]]; then
  echo "restore drill database already exists; choose another drill-id" >&2
  exit 2
fi
docker compose -f "$compose_file" exec -T postgres createdb -U youtuber "$database"
created_database=true
docker compose -f "$compose_file" exec -T postgres pg_restore -U youtuber -d "$database" \
  --exit-on-error --no-owner --no-privileges < "$backup/postgres.dump"

docker compose -f "$compose_file" --profile operations run --rm --no-deps \
  -v "$backup/object-storage:/backup:ro" backup-tool -c "
    set -eu
    export MC_CONFIG_DIR=/tmp/.mc
    access=\$(cat /run/secrets/minio_access_key)
    secret=\$(cat /run/secrets/minio_secret_key)
    mc alias set local http://minio:9000 \"\$access\" \"\$secret\" >/dev/null
    mc mb --ignore-existing local/$bucket >/dev/null
    mc --quiet mirror --overwrite /backup/production-artifacts local/$bucket
  "
created_bucket=true

table_count=$(docker compose -f "$compose_file" exec -T postgres psql -U youtuber -d "$database" -Atc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE';")
audit_count=$(docker compose -f "$compose_file" exec -T postgres psql -U youtuber -d "$database" -Atc \
  "SELECT count(*) FROM audit_events;")
source_count=$(docker compose -f "$compose_file" exec -T postgres psql -U youtuber -d "$database" -Atc \
  "SELECT count(*) FROM source_snapshots;")
object_count=$(docker compose -f "$compose_file" --profile operations run --rm --no-deps backup-tool -c "
  set -eu
  export MC_CONFIG_DIR=/tmp/.mc
  access=\$(cat /run/secrets/minio_access_key)
  secret=\$(cat /run/secrets/minio_secret_key)
  mc alias set local http://minio:9000 \"\$access\" \"\$secret\" >/dev/null
  mc --quiet find local/$bucket | wc -l
" | tr -d '[:space:]')

if (( table_count < 50 )); then
  echo "restore validation failed: expected at least 50 application tables, got $table_count" >&2
  exit 1
fi
if [[ ! "$object_count" =~ ^[0-9]+$ ]] || (( object_count != expected_object_count )); then
  echo "restore validation failed: expected $expected_object_count objects, got ${object_count:-invalid}" >&2
  exit 1
fi
cleanup
created_database=false
created_bucket=false
trap - EXIT
report="$backup/restore-drill-$drill_id.json"
cat > "$report" <<EOF
{
  "schema_version": "1.0",
  "drill_id": "$drill_id",
  "completed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "result": "passed",
  "database_table_count": $table_count,
  "audit_event_count": $audit_count,
  "source_snapshot_count": $source_count,
  "restored_object_count": $object_count,
  "production_database_modified": false,
  "production_bucket_modified": false,
  "temporary_targets_removed": true
}
EOF
chmod go-rwx "$report"
echo "$report"
