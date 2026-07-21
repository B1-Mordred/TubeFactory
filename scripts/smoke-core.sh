#!/usr/bin/env bash
set -euo pipefail

base_url="${BASE_URL:-http://localhost:8090}"
web_headers=$(mktemp)
web_body=$(mktemp)
trap 'rm -f "$web_headers" "$web_body"' EXIT

for dependency in curl jq; do
  command -v "$dependency" >/dev/null 2>&1 || {
    printf 'missing required command: %s\n' "$dependency" >&2
    exit 1
  }
done

ready_body="$(curl --fail --silent --show-error "${base_url}/api/health/ready")"
printf '%s\n' "$ready_body" | jq -e '
  .status == "ready" and
  .checks.postgres == "ready" and
  .checks.object_storage == "ready" and
  .checks.temporal == "ready"
' >/dev/null

bootstrap_body="$(curl --fail --silent --show-error "${base_url}/api/v1/auth/bootstrap-status")"
printf '%s\n' "$bootstrap_body" | jq -e '.required | type == "boolean"' >/dev/null

curl --fail --silent --show-error --dump-header "$web_headers" --output "$web_body" "${base_url}/"
csp_nonce=$(sed -n "s/^[Cc]ontent-[Ss]ecurity-[Pp]olicy:.*'nonce-\([^']*\)'.*/\1/p" "$web_headers" | head -n 1 | tr -d '\r')
csp_header=$(sed -n '/^[Cc]ontent-[Ss]ecurity-[Pp]olicy:/p' "$web_headers" | head -n 1 | tr -d '\r')
script_count=$(grep -o '<script[^>]*>' "$web_body" | wc -l)
nonced_script_count=$(grep -o "<script[^>]*nonce=\"${csp_nonce}\"[^>]*>" "$web_body" | wc -l)
if [[ -z "$csp_nonce" || "$script_count" -eq 0 || "$script_count" -ne "$nonced_script_count" ]]; then
  echo "UI bootstrap scripts do not match the response CSP nonce" >&2
  exit 1
fi
if [[ "$base_url" == http://* && "$csp_header" == *"upgrade-insecure-requests"* ]]; then
  echo "HTTP UI response must not force same-origin assets to HTTPS" >&2
  exit 1
fi

printf 'core smoke passed: api ready, UI bootstrap scripts CSP-authorized, bootstrap state available\n'
