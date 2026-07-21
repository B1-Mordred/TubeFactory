# Operator guide

## Local start

```bash
docker compose config --quiet
docker compose up --build -d
docker compose ps
curl --fail http://localhost:8090/api/health/ready
```

Navigate to <http://localhost:8090>. If no users exist, create the one-time bootstrap administrator. The bootstrap endpoint closes permanently after the first user is committed.

### LAN discovery and DHCP address

The default local deployment also accepts <http://evidence-studio.local:8090>. The host keeps its normal DHCP lease; a small hardened systemd/Avahi publisher maps the distinct application hostname to the current DHCP IPv4 address and advertises an `_http._tcp` service named **TubeFactory**. It checks the active address continuously and republishes after DHCP changes.

Install or refresh discovery on a systemd host after cloning the repository:

```bash
sudo install -m 0644 infra/lan/evidence-studio-mdns.service /etc/systemd/system/evidence-studio-mdns.service
sudo systemctl daemon-reload
sudo systemctl enable --now evidence-studio-mdns.service
avahi-resolve -n evidence-studio.local
avahi-browse -rt _http._tcp | grep -A3 'TubeFactory'
```

The checked-in unit targets `enp4s0`, `evidence-studio.local`, and port `8090`. If the deployment uses a different physical interface, name, or published port, update the unit through a systemd drop-in and keep `APP_SITE_ADDRESS`, `ALLOWED_HOSTS`, and `HTTP_PORT` in the Compose environment aligned. This deliberately uses the host's DHCP address instead of assigning an unmanaged address to a Docker container. Standard Docker Compose bridge networking has no DHCP IPAM driver, while the published Caddy port remains reachable on every host address.

Under **Authentication**, a local user can enroll TOTP after confirming the current password, confirm the first code, and store the displayed recovery codes offline. Each recovery code works once. An administrator can configure OIDC issuer/client/endpoints/scopes and an explicit group-to-role map; the client secret is write-only. Register the exact callback shown by the deployment (`/api/v1/auth/oidc/callback`) at the identity provider. Keep local administrator recovery access until the OIDC flow has been tested.

## Stop and restart

`docker compose down` stops containers while preserving named volumes. `docker compose up -d` resumes them. Never add `-v` unless deliberately destroying all local application data.

## Research profile

Start the core and current research services together:

```bash
docker compose --profile core --profile research up --build -d
docker compose --profile core --profile research ps
```

The profile adds private `searxng`, `research-worker`, and the self-hosted Firecrawl subsystem (API, Playwright, DNS/egress guards, PostgreSQL, Redis and RabbitMQ). None publishes a host port. SearXNG and the worker have controlled retrieval egress; the worker also joins the application/data networks so Temporal activities can persist metadata and objects. Firecrawl's browser is isolated from application/data networks and has no direct public route: DNS crosses a narrow forwarder and HTTP(S) crosses a public-address-pinning proxy. Live discovery, approved HTML, PDF, JSON, XML, CSV and text acquisition, JavaScript rendering fallback, exact-offset deterministic evidence extraction and reviewable dossier synthesis are available. The deterministic fixture path remains the repeatable offline dossier acceptance gate.

For a live opportunity, shortlist it as a reviewer, acquire its approved sources as an operator, then use **Build evidence dossier** after the opportunity reports immutable snapshots and `researching`. A dossier whose completion rules are unmet remains visibly blocked. Approval then requires a reviewer to enter the separately displayed reasoned override; the reason and failed criteria are retained in the audit event.

Each subject selects `assisted`, `supervised`, or `trusted` policy. Configure sensitive categories and evidence-density/repeated-scene limits in the same form. Sensitive/high-risk work always requires human dossier and final-video review, regardless of mode; publication always requires a reviewer. Before shortlisting any discovered or manual opportunity, provide the required **Why does this video deserve to exist?** rationale. The evaluated policy and rationale follow the approval lineage.

Every research start opens the durable workflow monitor. It streams state and progress through authenticated SSE, displays the request correlation ID, and reconstructs a safe activity log from Temporal without exposing inputs or provider errors. Operators may cancel a running workflow with an auditable reason. Failed, cancelled, terminated or timed-out runs expose **Retry as new run**; the retry receives a new workflow ID and an immutable parent edge, while repeating the same retry key reconciles the existing child.

Run the discovery/evidence acceptance gate against an administrator or operator account:

```bash
GATE_USERNAME=operator GATE_PASSWORD='replace-me' ./scripts/verify-discovery-fixture.sh
```

The script creates enabled acceptance-only profiles, validates a SearchPlan, runs durable fixture acquisition, inspects the cited dossier, and proves retry idempotency. It does not publish or call a paid service.

## Editorial production

Use **Providers & prompts** as an administrator to register a local or remote endpoint, its mounted secret reference, visible models and task capabilities. Activate exactly one route for `script_writer`, `script_verifier` and `storyboard`, and exactly one prompt per task. A verifier route must differ from the writer unless the assignment explicitly records the no-alternative exception. Remote endpoints require HTTPS; `remote_after_redaction` displays removed field paths/categories/value hashes in the safe usage ledger without returning the original values.

The site Compose model maps `ai.b1.germering` into the editorial worker through `AI_SERVER_IPV4` (default `192.168.2.100`). Override that value when the AI host receives a different LAN address; provider records should continue to use the distinguishable hostname rather than embedding the address.

The checked-in stack includes no provider credential. Add each credential as a root-readable secret file and mount it read-only into `editorial-worker`; set only the basename/reference in the UI. The worker rejects paths, symlinks, non-regular files and oversized secret values.

From **Scripts & storyboards**:

1. Generate from an exact approved dossier containing individually approved claims.
2. Inspect sentence annotations, claim badges, evidence excerpts, coverage and verifier issues. Unsupported factual text remains blocked and cannot be approved.
3. Manual edits create a draft immutable version. Run the independent verifier, then approve the exact verified version/hash. Select unlocked segments to regenerate; every unselected or locked segment is preserved exactly and the result again requires verification.
4. Generate a storyboard only from the exact approved script version. Scene edits clone a complete storyboard version. Locks reject edit/regeneration. **Regenerate** stores an immutable alternative; **Select** revalidates and clones the complete storyboard, after which a reviewer approves the exact new hash.

### GPU/media profile and split host

The `editorial-worker` is selectable through both the `core` and `gpu` profiles and joins the isolated `gpu` network. It contains the stable ComfyUI and Voicebox adapters; the GPU engines themselves remain optional and may run on the same machine or at authenticated LAN endpoints. Set `COMFYUI_ENDPOINT` in a site-specific Compose override and configure Voicebox profiles in the web UI. Do not publish engine ports on the public edge.

For a separate GPU host, run its approved, digest-pinned ComfyUI/Voicebox images with Compose on a private LAN, terminate authentication/TLS at that host, and point this stack at those endpoints. An NVIDIA-enabled engine service uses the Compose device reservation form below; apply it to the engine container, not to the adapter worker:

```yaml
services:
  your-gpu-engine:
    image: your-reviewed-image@sha256:replace-with-reviewed-digest
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
    networks: [gpu]
```

The checked-in core stack does not require the NVIDIA runtime. Treat the example image reference as a site-supplied value: resolve and review an immutable digest before deployment; never substitute `latest`.

Reproduce the code/runtime gate with:

```bash
docker compose --profile core run --rm api pytest -q -p no:cacheprovider /app/packages/core/tests
docker compose --profile core run --rm editorial-worker pytest -q -p no:cacheprovider /app/packages/ai-gateway/tests
docker compose --profile core run --rm api pytest -q -p no:cacheprovider /app/services/api/tests
docker compose --profile core run --rm editorial-worker pytest -q -p no:cacheprovider /app/services/editorial-worker/tests
docker compose --profile core build web
docker compose --profile core --profile research up -d
```

## Publishing profile

Publishing is absent from a normal core start. The deterministic dry-run path is available only after starting the explicit profile:

```bash
docker compose --profile core --profile publishing up --build -d
docker compose --profile core --profile publishing ps
GATE_USERNAME=admin GATE_PASSWORD='replace-me' ./scripts/verify-publishing-dry-run.sh
```

The gate selects an exact approved render, creates immutable source/chapter/caption/thumbnail metadata, records a private-upload approval, submits the same dry-run twice and proves one private mock video exists. It reconciles processing, then proves changed metadata cannot reuse the approval and that real mode is rejected while disabled.

For a real YouTube connection:

1. Create a Google web OAuth client, enable YouTube Data API v3 and register `https://your-studio.example/api/v1/publishing/oauth/callback` as an exact redirect URI.
2. Replace `youtube_oauth_client_id.dev`, `youtube_oauth_client_secret.dev`, `publication_encryption_key.dev` and `publisher_gateway_token.dev` through a production Compose override pointing to root-readable `*.local` files. Generate the encryption value as a URL-safe base64 encoding of 32 random bytes. Never rotate it without decrypting/re-encrypting stored connection and session values or reconnecting affected channels.
3. Start the publishing profile and use **Publishing & calendar → Connect YouTube with OAuth**. Consent requests `youtube.upload` and `youtube.force-ssl`; offline access is required for unattended reconciliation and an absent refresh token makes the connection fail closed.
4. Keep the active configuration in dry-run while validating metadata. An administrator must explicitly choose **Enable real private uploads** before a real request is accepted. A development mock credential is rejected by real mode.
5. Create metadata only from an exact final-approved render. A reviewer separately approves private upload. The operator queues the resumable upload, which always creates a private resource and persists its returned video ID. Reconcile until processing reaches `processed`.
6. A reviewer then records a distinct public-release approval for the exact same render and metadata hashes. Only then select a future time and schedule. Editing metadata creates a new version and invalidates both earlier bindings.

The worker never deletes or replaces a YouTube video automatically. If attachment or processing reconciliation fails after video creation, keep the private video and retry/reconcile the existing publication record.

The raw resumable protocol and state recovery follow Google's [resumable upload guide](https://developers.google.com/youtube/v3/guides/using_resumable_upload_protocol). Scheduling follows the `status.publishAt` constraint that the never-published video remain private. Caption upload uses the scope required by Google's `captions.insert` contract.

## Production prerequisites

Do not deploy the checked-in development secrets. Create root-readable `infra/compose/secrets/*.local` files, including separate Firecrawl PostgreSQL/admin values, use a production Compose override to reference them, set a real `APP_SITE_ADDRESS`, and let Caddy obtain or use explicitly configured TLS certificates. Restrict access to Temporal UI through an authenticated administrative route or a private management network. Keep Firecrawl API, queue management, browser, database, cache, broker, DNS and proxy ports internal-only.

Backups and a tested restore remain mandatory before publishing is enabled in a non-development environment. Create and verify them with unique, absolute paths outside the repository:

```bash
sudo ./scripts/backup.sh /srv/youtuber-backups/production-YYYYMMDDTHHMMSS
sudo ./scripts/restore-drill.sh /srv/youtuber-backups/production-YYYYMMDDTHHMMSS production-YYYYMMDD
sudo ./scripts/generate-sbom.sh /srv/youtuber-backups/sbom-YYYYMMDDTHHMMSS
sudo ./scripts/security-audit.sh
```

The scripts refuse overwrite/unsafe targets. The restore drill checks the backup manifest before creating a disposable database and bucket, validates exact object count and database contents, and removes the temporary targets. Keep `backup.json`, `MANIFEST.sha256`, the restore report and SBOM manifest together with off-host copies according to site retention policy.

Start observability with `OTEL_ENABLED=true docker compose --profile core --profile observability up -d`. Open **Analytics & operations → Open observability**; Grafana is authenticated and routed at `/grafana/`. Confirm all Prometheus targets are up, the API overview panels receive data, Loki has recent structured request logs, and the collector has application traces. Record backup, restore, SBOM, security and observability artifacts in the immutable operational-evidence UI.

## Durable workflow acceptance check

Start a durability probe from the System page or API, record its workflow ID, recreate `workflow-worker`, query the probe, signal it to complete, and confirm the final state. Use `scripts/verify-durable-workflow.sh` for the repeatable check.
