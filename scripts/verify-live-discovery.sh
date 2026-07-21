#!/usr/bin/env bash
set -euo pipefail

base_url="${BASE_URL:-http://localhost:8090}"
username="${GATE_USERNAME:?Set GATE_USERNAME to an operator or admin username}"
password="${GATE_PASSWORD:?Set GATE_PASSWORD to that account password}"

for dependency in curl jq; do
  command -v "$dependency" >/dev/null 2>&1 || {
    printf 'missing required command: %s\n' "$dependency" >&2
    exit 1
  }
done

cookie_file="$(mktemp)"
trap 'rm -f "$cookie_file"' EXIT
login="$(
  curl --fail --silent --show-error --cookie-jar "$cookie_file" \
    --header 'Content-Type: application/json' \
    --data "$(jq -n --arg username "$username" --arg password "$password" '{username:$username,password:$password}')" \
    "${base_url}/api/v1/auth/login"
)"
csrf="$(printf '%s' "$login" | jq -er .csrf_token)"
subject_id="${SUBJECT_PROFILE_ID:-$(curl --fail --silent --show-error --cookie "$cookie_file" "${base_url}/api/v1/subject-profiles" | jq -er '[.[] | select(.enabled)][0].id')}"
idempotency_key="live-gate-$(date -u +%Y%m%dT%H%M%S)-$$"

start="$(
  jq -n --arg subject "$subject_id" --arg key "$idempotency_key" \
    '{subject_profile_id:$subject,idempotency_key:$key}' \
  | curl --fail --silent --show-error --cookie "$cookie_file" \
      --header "X-CSRF-Token: ${csrf}" --header 'Content-Type: application/json' \
      --data @- "${base_url}/api/v1/research/live-discovery-runs"
)"
workflow_id="$(printf '%s' "$start" | jq -er .workflow_id)"

status=''
for _ in $(seq 1 60); do
  status="$(curl --fail --silent --show-error --cookie "$cookie_file" "${base_url}/api/v1/research/runs/${workflow_id}")"
  test "$(printf '%s' "$status" | jq -r .state)" = "OPPORTUNITY_REVIEW" && break
  sleep 1
done
printf '%s' "$status" | jq -e '
  .state == "OPPORTUNITY_REVIEW" and .progress == 100 and
  .result.search_strategy_count >= 3 and .result.raw_result_count >= .result.deduplicated_result_count and
  (.result.opportunity_ids | length) > 0
' >/dev/null

opportunities="$(curl --fail --silent --show-error --cookie "$cookie_file" "${base_url}/api/v1/research/opportunities")"
printf '%s' "$opportunities" | jq -e --argjson ids "$(printf '%s' "$status" | jq .result.opportunity_ids)" '
  [.[] | select(.id as $id | $ids | index($id)) | .source_count > 0] | length == ($ids | length) and all
' >/dev/null

retry="$(
  jq -n --arg subject "$subject_id" --arg key "$idempotency_key" \
    '{subject_profile_id:$subject,idempotency_key:$key}' \
  | curl --fail --silent --show-error --cookie "$cookie_file" \
      --header "X-CSRF-Token: ${csrf}" --header 'Content-Type: application/json' \
      --data @- "${base_url}/api/v1/research/live-discovery-runs"
)"
printf '%s' "$retry" | jq -e --arg workflow "$workflow_id" '.workflow_id == $workflow' >/dev/null

printf 'live discovery gate passed: %s produced %s provenance-linked pending opportunities\n' \
  "$workflow_id" "$(printf '%s' "$status" | jq -r '.result.opportunity_ids | length')"
