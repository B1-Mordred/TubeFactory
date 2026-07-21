# TubeFactory

TubeFactory is an evidence-first video production system.

This repository implements a self-hosted editorial production system that turns configurable research subjects into traceable, reviewed, privately uploaded YouTube videos. Accuracy, provenance, restart safety, and human authorization are hard product constraints.

The local application is discoverable at `http://evidence-studio.local:8090` after installing the checked-in DHCP-aware mDNS unit; `http://localhost:8090` remains available on the host. See the LAN discovery section in `docs/operator-guide.md`.

The implementation is delivered in acceptance-gated increments. The current state is recorded in [`docs/implementation-status.md`](docs/implementation-status.md); unavailable features are labelled and disabled rather than simulated.

## Development start

Prerequisites: Docker Engine 29+ with Compose 2.40+ and at least 8 GB of free RAM.

```bash
docker compose up --build -d
docker compose ps
curl --fail http://localhost:8090/api/health/ready
```

Open <http://localhost:8090>. On an empty database the UI offers a one-time administrator bootstrap flow.

The checked-in `.env` and `*.dev` secret files are public, development-only defaults. Before any non-local deployment, follow [`docs/operator-guide.md`](docs/operator-guide.md) to generate private secret files and enable TLS.

## Repository map

- `apps/web`: Next.js/React operator UI
- `services/api`: FastAPI delivery and infrastructure adapters
- `services/workflow-worker`: Temporal workflows and activities
- `services/research-worker`: isolated live/fixture discovery and source-acquisition activities
- `services/editorial-worker`: isolated model gateway, script verification and storyboard activities
- `services/publisher-worker`: private-first YouTube OAuth, resumable upload, reconciliation and scheduling activities
- `packages/core`: framework-independent domain entities, policies, and ports
- `packages/contracts`: versioned external JSON Schemas
- `infra/compose`: Compose topology and service configuration
- `docs`: architecture, security, data model, operations, ADRs, and status

Use `make help` for reproducible validation commands.

## Branch workflow

Use `dev` for ongoing development. Keep the local checkout current with `git pull --ff-only origin dev`, commit development work on `dev`, and promote reviewed changes to `main` through pull requests.

To start the current research profile and run its deterministic evidence gate:

```bash
make up-research
GATE_USERNAME=operator GATE_PASSWORD='replace-me' ./scripts/verify-discovery-fixture.sh
GATE_USERNAME=operator GATE_PASSWORD='replace-me' ./scripts/verify-live-discovery.sh
```

The live gate is intentionally not a CI test: public search engines are nondeterministic and may throttle a self-hosted SearXNG instance. Its output always enters the pending Opportunity Board; it does not trigger acquisition, generation, upload, or publication.

Publishing remains disabled until its profile is selected. The deterministic acceptance gate performs no Google request and never creates a public video:

```bash
docker compose --profile core --profile publishing up --build -d
GATE_USERNAME=admin GATE_PASSWORD='replace-me' ./scripts/verify-publishing-dry-run.sh
```

For the complete stack and final operational gates:

```bash
OTEL_ENABLED=true docker compose --profile core --profile research --profile publishing --profile observability up --build -d
sudo ./scripts/security-audit.sh
sudo ./scripts/backup.sh /srv/youtuber-backups/backup-YYYYMMDDTHHMMSS
sudo ./scripts/restore-drill.sh /srv/youtuber-backups/backup-YYYYMMDDTHHMMSS acceptance-YYYYMMDD
sudo ./scripts/generate-sbom.sh /srv/youtuber-backups/sbom-YYYYMMDDTHHMMSS
```

All normal configuration, operating-profile decisions, editorial work, approvals, recovery evidence and identity setup are available through the web UI. Real Google upload remains disabled until an administrator supplies site-owned OAuth secrets, explicitly enables real private upload, and a reviewer separately authorizes the exact release.
