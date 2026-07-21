#!/usr/bin/env bash
set -euo pipefail

base_url="${BASE_URL:-http://localhost:8090}"
username="${GATE_USERNAME:?Set GATE_USERNAME to an administrator username}"
password="${GATE_PASSWORD:?Set GATE_PASSWORD to that account password}"

for dependency in curl jq; do
  command -v "$dependency" >/dev/null 2>&1 || {
    printf 'missing required command: %s\n' "$dependency" >&2
    exit 1
  }
done

cookie_file="$(mktemp)"
response_file="$(mktemp)"
trap 'rm -f "$cookie_file" "$response_file"' EXIT

login="$(
  curl --fail --silent --show-error --cookie-jar "$cookie_file" \
    --header 'Content-Type: application/json' \
    --data "$(jq -n --arg username "$username" --arg password "$password" '{username:$username,password:$password}')" \
    "${base_url}/api/v1/auth/login"
)"
csrf="$(printf '%s' "$login" | jq -er .csrf_token)"
suffix="$(date -u +%Y%m%dT%H%M%S)-$$"

get() {
  curl --fail --silent --show-error --cookie "$cookie_file" "$1"
}
post() {
  curl --fail --silent --show-error --cookie "$cookie_file" \
    --header "X-CSRF-Token: ${csrf}" --header 'Content-Type: application/json' \
    --data "$2" "$1"
}

config="$(get "${base_url}/api/v1/publishing/configuration")"
if test "$(printf '%s' "$config" | jq -r .version_number)" = 0 || \
   test "$(printf '%s' "$config" | jq -r .real_uploads_enabled)" = true; then
  config="$(post "${base_url}/api/v1/publishing/configuration" \
    '{"real_uploads_enabled":false,"provider":"youtube","comment":"Acceptance gate forces publishing into dry-run mode."}')"
fi
printf '%s' "$config" | jq -e '.real_uploads_enabled == false' >/dev/null

channel_id="$(get "${base_url}/api/v1/channel-profiles" | jq -er '.[0].id')"
connection="$(post "${base_url}/api/v1/publishing/connections/mock" \
  "$(jq -n --arg channel "$channel_id" --arg suffix "$suffix" '{channel_profile_id:$channel,youtube_channel_id:("mock_" + ($suffix|gsub("[^A-Za-z0-9_-]";"_"))),youtube_channel_title:"Publishing acceptance mock"}')")"
connection_id="$(printf '%s' "$connection" | jq -er .id)"

productions="$(get "${base_url}/api/v1/media/productions")"
production="$(printf '%s' "$productions" | jq -ec '[.[] | select(
  .render != null and .manifest != null and .approval.decision == "approved" and
  ([.assets[].asset_kind] | index("thumbnail") != null) and
  (([.assets[].asset_kind] | index("caption_vtt") != null) or ([.assets[].asset_kind] | index("caption_srt") != null))
)] | first')"
render_id="$(printf '%s' "$production" | jq -er .render.id)"
render_hash="$(printf '%s' "$production" | jq -er .render.content_hash)"
caption_id="$(printf '%s' "$production" | jq -er '[.assets[] | select(.asset_kind == "caption_vtt" or .asset_kind == "caption_srt")][0].id')"
thumbnail_id="$(printf '%s' "$production" | jq -er '[.assets[] | select(.asset_kind == "thumbnail")][0].id')"

metadata_payload="$(printf '%s' "$production" | jq -c \
  --arg render "$render_id" --arg render_hash "$render_hash" --arg caption "$caption_id" \
  --arg thumbnail "$thumbnail_id" --arg suffix "$suffix" '{
    render_id:$render,expected_render_hash:$render_hash,
    title:("Publishing acceptance " + $suffix),
    description:"A deterministic dry-run assembled from exact approved production artifacts.",
    sources:[.manifest.document.sources[] | {title,url}],evidence_url:null,
    chapters:[.manifest.document.chapters[] | {start_seconds:((.start_seconds // .timecode_seconds)|floor),title}],
    tags:["evidence","acceptance"],category_id:"27",language:"en",made_for_kids:false,
    contains_synthetic_media:any(.manifest.document.scenes[]?; .scene_spec.synthetic_media_flag == true),
    captions:{asset_id:$caption,language:"en",name:"English"},thumbnail:{asset_id:$thumbnail},
    comment:"Acceptance metadata is bound to the exact approved render and artifacts."
  }')"
metadata="$(post "${base_url}/api/v1/publishing/metadata" "$metadata_payload")"
metadata_id="$(printf '%s' "$metadata" | jq -er .id)"
metadata_hash="$(printf '%s' "$metadata" | jq -er .content_hash)"

approval_payload="$(jq -n --arg render "$render_id" --arg render_hash "$render_hash" \
  --arg metadata "$metadata_id" --arg metadata_hash "$metadata_hash" '{
    purpose:"private_upload",render_id:$render,expected_render_hash:$render_hash,
    metadata_version_id:$metadata,expected_metadata_hash:$metadata_hash,decision:"approved",
    comment:"Acceptance reviewer approves this exact private dry-run upload binding."
  }')"
post "${base_url}/api/v1/publishing/approvals" "$approval_payload" >/dev/null

upload_payload="$(jq -n --arg connection "$connection_id" --arg render "$render_id" \
  --arg render_hash "$render_hash" --arg metadata "$metadata_id" --arg metadata_hash "$metadata_hash" \
  --arg suffix "$suffix" '{connection_id:$connection,render_id:$render,expected_render_hash:$render_hash,
  metadata_version_id:$metadata,expected_metadata_hash:$metadata_hash,mode:"dry_run",idempotency_key:("dry-run-"+$suffix)}')"
first="$(post "${base_url}/api/v1/publishing/uploads" "$upload_payload")"
retry_payload="$(printf '%s' "$upload_payload" | jq -c '.idempotency_key += "-retry"')"
second="$(post "${base_url}/api/v1/publishing/uploads" "$retry_payload")"
publication_id="$(printf '%s' "$first" | jq -er .id)"
test "$publication_id" = "$(printf '%s' "$second" | jq -er .id)"

status=''
for _ in $(seq 1 60); do
  status="$(get "${base_url}/api/v1/publishing/uploads/${publication_id}")"
  case "$(printf '%s' "$status" | jq -r .state)" in
    uploaded_private|processed|failed) break ;;
  esac
  sleep 1
done
printf '%s' "$status" | jq -e '
  .state == "uploaded_private" and .mode == "dry_run" and
  (.youtube_video_id | startswith("dry_")) and
  .caption_status.status == "completed" and .thumbnail_status.status == "completed"
' >/dev/null

post "${base_url}/api/v1/publishing/uploads/${publication_id}/reconcile" '{}' >/dev/null
for _ in $(seq 1 30); do
  status="$(get "${base_url}/api/v1/publishing/uploads/${publication_id}")"
  test "$(printf '%s' "$status" | jq -r .state)" = processed && break
  sleep 1
done
printf '%s' "$status" | jq -e '.state == "processed" and .processing_status.status.privacyStatus == "private"' >/dev/null

changed_payload="$(printf '%s' "$metadata_payload" | jq -c '.title += " changed" | .comment="Changed metadata must invalidate the earlier approval."')"
changed="$(post "${base_url}/api/v1/publishing/metadata" "$changed_payload")"
changed_upload="$(printf '%s' "$upload_payload" | jq -c \
  --arg id "$(printf '%s' "$changed" | jq -er .id)" \
  --arg hash "$(printf '%s' "$changed" | jq -er .content_hash)" \
  '.metadata_version_id=$id | .expected_metadata_hash=$hash | .idempotency_key += "-changed"')"
changed_code="$(curl --silent --show-error --output "$response_file" --write-out '%{http_code}' \
  --cookie "$cookie_file" --header "X-CSRF-Token: ${csrf}" --header 'Content-Type: application/json' \
  --data "$changed_upload" "${base_url}/api/v1/publishing/uploads")"
test "$changed_code" = 409
jq -e '.detail | contains("separate exact-version approval")' "$response_file" >/dev/null

real_payload="$(printf '%s' "$upload_payload" | jq -c '.mode="real" | .idempotency_key += "-real"')"
real_code="$(curl --silent --show-error --output "$response_file" --write-out '%{http_code}' \
  --cookie "$cookie_file" --header "X-CSRF-Token: ${csrf}" --header 'Content-Type: application/json' \
  --data "$real_payload" "${base_url}/api/v1/publishing/uploads")"
test "$real_code" = 403
jq -e '.detail | contains("disabled by active admin configuration")' "$response_file" >/dev/null

printf 'publishing dry-run gate passed: one idempotent private video %s for publication %s; changed metadata and real mode were blocked\n' \
  "$(printf '%s' "$status" | jq -er .youtube_video_id)" "$publication_id"
