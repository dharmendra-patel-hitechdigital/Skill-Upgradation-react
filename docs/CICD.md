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

## Base images come from ECR Public, not Docker Hub

Every base image resolves through the ECR Public mirror of the Docker Official
Images:

| Used for | Image |
|---|---|
| API builder + runtime | `public.ecr.aws/docker/library/python:3.12-slim` |
| SPA build stage | `public.ecr.aws/docker/library/node:20-alpine` |
| SPA serve stage, edge proxy | `public.ecr.aws/docker/library/nginx:1.27-alpine` |
| Local MySQL | `public.ecr.aws/docker/library/mysql:8.4` |

**Why.** Docker Hub enforces its anonymous pull limit **per source IP**, and
CodeBuild egresses through shared NAT addresses. So an unauthenticated
`FROM python:3.12-slim` fails like this, on traffic nobody in this project
generated:

```
ERROR: failed to solve: unexpected status from HEAD request to
https://registry-1.docker.io/v2/library/python/manifests/3.12-slim:
429 Too Many Requests
```

That is a red pipeline with a green codebase, and re-running it is a coin flip.
ECR Public applies no such limit to pulls from AWS and serves the same digests,
so this is a routing change, not a version change.

Two smaller cuts in the same direction:

* The `# syntax=docker/dockerfile:1` directive is **gone** from both
  Dockerfiles. It made every build resolve `docker/dockerfile:1` from Docker Hub
  before reading the file — a second rate-limited request per build, for a
  frontend whose features this project does not use. If you ever need a newer
  frontend, pass `--build-arg BUILDKIT_SYNTAX=<mirrored ref>` rather than
  putting the directive back.
* The free-tier deploy bundle's `edge` service pulls the mirror too. It was the
  only image in that bundle not coming from ECR, which made it the only one that
  could 429 — and it does so at *deploy* time, after tests have passed, on an
  instance nobody is watching.

**Overriding.** Nothing is pinned to one registry. Each image is a build arg
with an environment-variable escape hatch, so switching back to Docker Hub (or
to an [ECR pull-through cache](https://docs.aws.amazon.com/AmazonECR/latest/userguide/pull-through-cache.html),
which is the better answer at higher volume) is one variable:

```bash
PYTHON_IMAGE=python:3.12-slim ./scripts/ci.sh test
NGINX_IMAGE=123456789012.dkr.ecr.us-east-1.amazonaws.com/dockerhub/library/nginx:1.27-alpine \
  docker compose up
```

If you must keep pulling from Docker Hub, authenticate the build instead of
mirroring — `docker login` with a Hub account raises the limit substantially and
scopes it to the account rather than the NAT address.

Anonymous ECR Public pulls are enough for one build per commit, and need no
credentials — which is why the buildspecs were left alone. If you later run many
concurrent builds and start seeing throttling from `public.ecr.aws` too,
authenticating raises the ceiling:

```bash
aws ecr-public get-login-password --region us-east-1 \
  | docker login --username AWS --password-stdin public.ecr.aws
```

That needs `ecr-public:GetAuthorizationToken` and `sts:GetServiceBearerToken` on
the CodeBuild role, so it is a pipeline-stack change — not worth making until
the throttling is real.

### Registry reads are retried

`scripts/ci.sh` wraps every `docker build`/`docker push` in a three-attempt
retry with exponential backoff, because registry reads fail transiently even
without a rate limit (TLS resets, CDN 5xx, DNS blips). Builds and pushes are
idempotent, so retrying is safe.

The **test run itself is deliberately not retried** — a failing suite must fail
once, not three times. That is also why the test image is now built as its own
step before `run`: when `run` did the pull, a 429 was indistinguishable in the
log from a broken test.

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
