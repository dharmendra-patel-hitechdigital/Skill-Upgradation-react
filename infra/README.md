# Deployment runbook

> **Looking to spend nothing?** This design costs ~$90/month — NAT Gateway,
> ALB, and Fargate have no free allowance between them. See
> [free-tier/README.md](free-tier/README.md) for the same application on one
> `t3.micro` for ~$0.

AWS CodePipeline builds both services as Docker images, pushes them to ECR, runs
Alembic against RDS, and rolls them out on ECS Fargate behind a single
Application Load Balancer.

```
GitHub  ──▶  CodePipeline
                 │
                 ├─ Build ──┬─ CodeBuild: lint + pytest + docker build ─▶ ECR (api)
                 │          └─ CodeBuild: npm build + docker build ─────▶ ECR (web)
                 │             (both run in parallel)
                 │
                 ├─ Migrate ── CodeBuild in-VPC: alembic upgrade head ──▶ RDS
                 │
                 └─ Deploy ──┬─ ECS rolling update: api
                             └─ ECS rolling update: web
```

At runtime one ALB fronts both services, so the browser talks to a single
origin:

```
ALB :80
 ├── /api/*, /docs, /redoc, /openapi.json  ──▶  api  (FastAPI, :8000)
 └── /*                                    ──▶  web  (nginx, :80)
```

That is why `VITE_API_BASE_URL` defaults to `/api/v1` rather than a hostname:
requests are same-origin, so no CORS preflight and no environment hostname
baked into the JS bundle.

## Files

| Path | Purpose |
|---|---|
| `cloudformation/10-network.yaml` | VPC, subnets, NAT, ALB, ECR repos, ECS cluster |
| `cloudformation/20-data.yaml` | RDS MySQL, S3 document bucket, Secrets Manager |
| `cloudformation/30-services.yaml` | Task definitions, ECS services, target groups, autoscaling |
| `cloudformation/40-pipeline.yaml` | CodePipeline, three CodeBuild projects, IAM |
| `buildspec/migrate.yml` | Alembic stage, runs inside the VPC |
| `scripts/deploy.sh` | Deploys the stacks in dependency order |
| `scripts/bootstrap-images.sh` | One-time first image push |
| `local/edge.nginx.conf` | Stands in for the ALB in `docker-compose.yml` |
| `../Python/buildspec.yml` | API build: lint, test, image |
| `../React/buildspec.yml` | SPA build: bundle, image |

## First deployment

Run from Git Bash on Windows. Needs AWS CLI v2 (authenticated) and Docker.

```bash
cp .env.example .env      # set GITHUB_OWNER / GITHUB_REPO / AWS_REGION
set -a; source .env; set +a

./infra/scripts/deploy.sh network          # ~5 min (NAT gateway is the slow part)
./infra/scripts/deploy.sh data             # ~10 min (RDS)
./infra/scripts/bootstrap-images.sh        # see "the ordering problem" below
./infra/scripts/deploy.sh services         # ~5 min
./infra/scripts/deploy.sh pipeline         # ~2 min
```

Then complete the GitHub connection — CloudFormation creates it in `PENDING`
and **cannot** finish the OAuth handshake for you:

*Developer Tools → Settings → Connections → skill-upgradation-github → Update
pending connection*

Push to `main` and the pipeline runs. Get the URL with:

```bash
./infra/scripts/deploy.sh outputs network
```

### The ordering problem

`bootstrap-images.sh` exists because the dependency is circular: the service
stack creates ECS services whose task definitions reference an image tag, but
the pipeline can only deploy to services that already exist. One manual push
breaks the cycle. After that, every image comes from CodePipeline — `deploy.sh
services` refuses to run against an empty ECR repo rather than letting the
stack hang until it times out.

## How secrets reach the app

Nothing sensitive is a CloudFormation parameter, so no secret appears in a
stack event, a shell history, or a diff.

* `20-data.yaml` **generates** the RDS password and `SECRET_KEY` into Secrets
  Manager.
* The ECS task definition injects them as individual environment variables via
  `Secrets` / `ValueFrom`.
* `OPENAI_API_KEY` is created empty on purpose. Fill it in when you want it:

  ```bash
  aws secretsmanager put-secret-value \
    --secret-id skill-upgradation/app/secrets \
    --secret-string '{"SECRET_KEY":"<keep the existing value>","OPENAI_API_KEY":"sk-..."}'
  ```

  Until then the API falls back to its offline analyzer. Force a pickup with
  `aws ecs update-service --force-new-deployment` — secrets are read at task
  start, not polled.

A task definition cannot splice a secret into the middle of a string, which is
why the app takes `DB_HOST` / `DB_USER` / `DB_PASSWORD` separately and assembles
`DATABASE_URL` itself (`app/core/config.py`). That also percent-encodes the
generated password, so a `#` or `/` in it cannot silently corrupt the URL.

## Migrations

The `Migrate` stage runs between build and deploy. Keep migrations **additive**:
an ECS rolling update runs old and new tasks side by side for a few minutes, so
the schema has to satisfy both. Drop a column in a later release, not the same
one that stops writing to it.

The stage runs in a private subnet because RDS is not publicly accessible; that
subnet needs its NAT route, or the build hangs reaching PyPI and Secrets Manager.

## Safety rails already wired up

* **Deployment circuit breaker** with rollback on both services — a bad image
  rolls back to the last working task definition instead of crash-looping.
* **`MinimumHealthyPercent: 100`** — capacity never dips during a deploy.
* **Readiness-based routing** — the API target group checks
  `/api/v1/health/ready`, which checks the database, so a task with a broken
  connection is pulled from the load balancer rather than serving errors.
* **RDS `DeletionPolicy: Snapshot`** and a retained S3 bucket — deleting the
  wrong stack costs time, not data.
* Tasks run in private subnets with `AssignPublicIp: DISABLED`, reachable only
  from the ALB's security group.

## Cost notes

The two standing costs are the NAT gateway (~$32/mo) and RDS. For a
non-production environment:

* `SingleNatGateway=true` (the default) — one NAT instead of two.
* `MultiAz=false` (the default) and `DbInstanceClass=db.t4g.micro`.
* Scale to zero when idle:
  ```bash
  aws ecs update-service --cluster skill-upgradation-cluster \
    --service skill-upgradation-api --desired-count 0
  ```

The S3 gateway endpoint in `10-network.yaml` is free and keeps ECR layer pulls
and document traffic off the NAT gateway, where they would be billed per GB.

## Before this is production

The template set stops deliberately short of things that need decisions only you
can make:

* **HTTPS.** The listener is HTTP only. Add an ACM certificate, a port-443
  listener, and redirect 80 → 443. Until then `Secure` cookies and HSTS are not
  meaningful.
* **`DeletionProtection: true`** on the RDS instance.
* **`/docs` exposure.** `ApiDocsRule` publishes the OpenAPI schema. Remove the
  rule if that should not be public.
* **A manual approval stage** in the pipeline before `Deploy`, if you want a
  human gate.
* **mypy is non-gating** in `Python/buildspec.yml` (`|| true`). Remove that once
  the tree is clean, or every build normalises ignoring it.
