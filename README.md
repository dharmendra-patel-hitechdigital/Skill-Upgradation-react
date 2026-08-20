# Skill-Upgradation

An AI document-processing service (FastAPI + MySQL) with a React dashboard,
deployed to AWS ECS Fargate by CodePipeline.

| Directory | What it is |
|---|---|
| [Python/](Python/) | Async FastAPI backend — JWT auth, document upload, AI extraction. [README](Python/README.md) |
| [React/](React/) | Vite + React SPA — login and dashboard. [README](React/README.md) |
| [infra/](infra/) | CloudFormation, buildspecs, deploy scripts. [Runbook](infra/README.md) |

## Run the whole thing locally

```bash
cp .env.example .env          # optional: OPENAI_API_KEY
docker compose up --build
```

* App — <http://localhost:8080>
* API docs — <http://localhost:8080/docs>

The `edge` container in [docker-compose.yml](docker-compose.yml) plays the part
of the AWS load balancer, routing `/api/*` to the backend and everything else to
the SPA. That keeps the local topology honest: the frontend is built against the
same same-origin `/api/v1` base URL it uses in production, so a CORS or routing
mistake shows up here rather than after a deploy.

To work on one side alone, each directory still runs standalone — see
[Python/README.md](Python/README.md) and [React/README.md](React/README.md).

## Deploy to AWS

Two paths, same application and same images — pick by budget:

| | [Free tier](infra/free-tier/README.md) | [Production](infra/README.md) |
|---|---|---|
| Hosting | 1× EC2 `t3.micro` + Docker Compose | ECS Fargate, 2 services |
| Database | RDS `db.t4g.micro` | RDS, optional Multi-AZ |
| Routing | nginx on the instance | Application Load Balancer |
| Secrets | SSM Parameter Store (free) | Secrets Manager |
| Cost | ~$0 for 12 months | ~$90/month |
| Deploys | in-place restart, brief 502 | rolling, zero-downtime, auto-rollback |
| Needs Docker locally | No | Yes, for the first image push |

Start on the free tier; move to Fargate when downtime or a single point of
failure stops being acceptable. Both are driven by CodePipeline and build from
the same `Dockerfile`s.

```bash
# Production
./infra/scripts/deploy.sh network
./infra/scripts/deploy.sh data
./infra/scripts/bootstrap-images.sh
./infra/scripts/deploy.sh services
./infra/scripts/deploy.sh pipeline
```

After that, pushing to `main` builds, tests, migrates, and rolls out both
services automatically.

## Tests

Containerized, so the only requirement is Docker:

```bash
docker compose -f docker-compose.ci.yml run --rm tests
```

That runs lint and the suite inside the same image the API ships as. Every
pipeline runs this exact command, so a green run locally means a green run in
CI.

Natively, if you prefer:

```bash
cd Python
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

A failing test means no image is pushed, so a red build cannot produce an
artifact someone deploys later by hand.

## CI/CD

Three pipelines share one build definition in [scripts/ci.sh](scripts/ci.sh):

| Pipeline | Trigger | Target |
|---|---|---|
| [GitHub Actions](.github/workflows/) | PRs and `main` | ECS Fargate, via OIDC (no stored AWS keys) |
| [CodePipeline](infra/cloudformation/40-pipeline.yaml) | `main` | ECS Fargate |
| [Free-tier pipeline](infra/free-tier/buildspec.yml) | `main` | single EC2 host |

See [docs/CICD.md](docs/CICD.md) for how they fit together and how to add
another CI system in about fifteen lines.
