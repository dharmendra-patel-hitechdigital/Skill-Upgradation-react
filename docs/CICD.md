# CI/CD

Three pipelines, one build definition. They all shell out to
[`scripts/ci.sh`](../scripts/ci.sh), so "how this project builds" is written
once.

```
                      ┌──────────────────────────────┐
                      │  scripts/ci.sh               │
                      │  test │ build │ push         │
                      │  (Docker only, no vendor SDK)│
                      └──────────────┬───────────────┘
                                     │
        ┌────────────────────────────┼────────────────────────────┐
        │                            │                            │
 GitHub Actions              AWS CodePipeline            Free-tier pipeline
 .github/workflows/          Python|React/buildspec.yml  infra/free-tier/
   ci.yml     (PRs)            → ECR → migrate → ECS       buildspec.yml
   deploy.yml (main)                                       → ECR → SSM → EC2
```

## Why a shared script

Build logic previously lived in three buildspecs. Adding GitHub Actions would
have made a fourth copy of the same `docker build` invocations, including the
non-obvious parts — `--target runtime`, the `VITE_API_BASE_URL` build arg, the
provenance labels. Copies drift, and the drift shows up as "works in CodeBuild,
broken in Actions".

Each vendor config now owns only what genuinely differs: **authentication** and
**triggering**. Everything else is delegated.

```bash
./scripts/ci.sh test                  # lint + tests, containerized
./scripts/ci.sh build <tag> [api|web] # build (default: both)
./scripts/ci.sh push  <tag> [api|web] # push
./scripts/ci.sh images <tag>          # print image URIs
```

The `api|web` argument exists because CodePipeline runs one CodeBuild project
per service in parallel — without it, each project would rebuild both images.

## Tests run in a container

[`docker-compose.ci.yml`](../docker-compose.ci.yml) runs lint and the suite
inside the `test` stage of [`Python/Dockerfile`](../Python/Dockerfile), which
layers `pytest`/`ruff` on top of the **runtime** image:

```bash
docker compose -f docker-compose.ci.yml run --rm tests
```

Two things this buys:

1. **No toolchain on the runner.** No `setup-python`, no `setup-node`, no
   version pinning per CI system. Only Docker.
2. **Tests exercise the shipped image.** Same interpreter, same wheels, same
   file layout. The usual "passed in CI, crashed in the container" gap comes
   from testing somewhere the app never actually runs.

The `test` stage is not in the default build — `docker build` without
`--target` stops at `runtime`, so none of it reaches the deployed image.

## GitHub Actions

| Workflow | Trigger | Does |
|---|---|---|
| [ci.yml](../.github/workflows/ci.yml) | PRs, non-main pushes | lint + test, build both images (no push), cfn-lint |
| [deploy.yml](../.github/workflows/deploy.yml) | push to `main` | test → push to ECR → migrate → roll out ECS → smoke test |

`ci.yml` touches no AWS account and needs no credentials, so it is safe on fork
PRs.

### Setup

```bash
aws cloudformation deploy \
  --stack-name skill-upgradation-github-oidc \
  --template-file infra/cloudformation/50-github-oidc.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
      ProjectName=skill-upgradation \
      GitHubOwner=your-username \
      GitHubRepo=Skill-Upgradation \
      AllowedRefPattern=refs/heads/main
```

Then **Settings → Secrets and variables → Actions → Variables**:

| Variable | Value |
|---|---|
| `AWS_DEPLOY_ROLE_ARN` | the stack's `DeployRoleArn` output |
| `AWS_REGION` | e.g. `us-east-1` |

Variables, not secrets — neither is sensitive, and a role ARN visible in the log
is easier to debug than a masked one.

### Why OIDC and not access keys

GitHub mints a short-lived signed token; STS exchanges it for credentials that
expire in an hour. A leaked `AWS_SECRET_ACCESS_KEY` in repository secrets stays
valid until somebody notices and rotates it. A leaked OIDC token is useless
minutes later, and only ever worked from the one repo and ref named in the trust
policy.

The `AllowedRefPattern` parameter is what enforces that. It defaults to
`refs/heads/main` — keep it narrow. `refs/heads/*` would let any branch deploy
to production.

## Migrations

GitHub-hosted runners **cannot reach a private RDS instance**, so
[`scripts/aws-migrate.sh`](../scripts/aws-migrate.sh) runs `alembic upgrade
head` as a one-off ECS task inside the VPC, using the API image about to be
deployed. It waits for the task, prints its CloudWatch logs, and fails on a
non-zero exit code.

The alternative — opening the database security group to GitHub's IP ranges —
would be a permanent hole for a two-minute job.

Keep migrations **additive**. A rolling ECS update runs old and new tasks
side by side for a few minutes, so the schema must satisfy both. Drop a column
in a later release than the one that stops writing to it.

## Both AWS pipelines, on purpose

CodePipeline and GitHub Actions both deploy to the same ECS services from the
same ECR repos. Whichever ran last is live. That is deliberate:

- **Actions** gives faster feedback and PR-time validation.
- **CodePipeline** keeps a deploy path that works when GitHub is unreachable,
  and runs entirely inside your AWS account.

If you would rather have one, delete the other — the shared `scripts/ci.sh`
means neither is load-bearing for the other.

## Adding another CI system

Roughly fifteen lines. Authenticate to the registry, then:

```yaml
# GitLab CI, for example
build:
  image: docker:27
  services: [docker:27-dind]
  variables:
    REGISTRY: $CI_REGISTRY_IMAGE
  script:
    - ./scripts/ci.sh test
    - ./scripts/ci.sh build "$CI_COMMIT_SHORT_SHA"
    - ./scripts/ci.sh push "$CI_COMMIT_SHORT_SHA"
```

`REGISTRY` and `PROJECT_NAME` are the only inputs `ci.sh` needs.
