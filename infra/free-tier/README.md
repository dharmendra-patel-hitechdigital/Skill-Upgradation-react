# Free-tier deployment

A deliberately cheaper path than [the Fargate stacks](../README.md): the whole
application runs as Docker Compose on **one `t3.micro` EC2 instance**, with a
**`db.t4g.micro` RDS** database beside it. Same images, same pipeline shape,
same code — only the hosting target changes.

```
Internet
   │  :80
   ▼
EC2 t3.micro  (public subnet, Elastic IP)
   ├── edge   nginx      →  /api/*  ▸ api      everything else ▸ web
   ├── api    FastAPI :8000
   └── web    nginx SPA :80
                  │
                  ▼
        RDS db.t4g.micro  (private subnets, no internet route)
```

**No NAT Gateway, no ALB, no Fargate** — those three are what make the
production design cost ~$90/month, and none of them has any free allowance.

## What this costs

| Service | Allowance | This stack uses |
|---|---|---|
| EC2 `t3.micro` | 750 hrs/mo · 12 mo | 1 instance = 730 hrs ✅ |
| RDS `db.t4g.micro` | 750 hrs + 20 GB · 12 mo | 1 instance, 20 GB ✅ |
| EBS | 30 GB · 12 mo | 20 GB root volume ✅ |
| ECR | 500 MB · 12 mo | ~400 MB (3-image lifecycle cap) ⚠️ |
| S3 | 5 GB · 12 mo | a few MB ✅ |
| CodeBuild | **100 min/mo · always free** | ~6 min/build ≈ 15 builds ⚠️ |
| CodePipeline | **1 pipeline/mo · always free** | 1 (V1) ✅ |
| SSM Parameter Store | **Standard params · always free** | 3 params ✅ |
| CloudWatch Logs | 5 GB/mo · always free | small ✅ |
| Data transfer out | 100 GB/mo · always free | small ✅ |
| Elastic IP | Free **while attached to a running instance** | 1 ⚠️ |

> **Verify these numbers before relying on them.** AWS restructured its free
> tier in 2025 — newer accounts get a credit-based plan rather than the classic
> 12-month allowances, and the details change. Check what *your* account has:
>
> - Your free tier usage: <https://console.aws.amazon.com/billing/home#/freetier>
> - Free tier overview: <https://aws.amazon.com/free/>
> - Free tier FAQs: <https://aws.amazon.com/free/free-tier-faqs/>

**Set a budget alarm before you deploy anything.** It is the single thing that
turns a surprise bill into an email:
<https://console.aws.amazon.com/costmanagement/home#/budgets> — create a **Zero
spend budget**, which alerts the moment any charge appears.

The three things most likely to start billing: the EC2 instance after 12
months, ECR if the lifecycle rule is loosened, and the Elastic IP if you stop
the instance without releasing it.

## Deploy

### 1. Prerequisites

- AWS CLI v2, authenticated — <https://awscli.amazonaws.com/AWSCLIV2.msi>
- Your code pushed to GitHub
- **No Docker needed locally.** Unlike the Fargate path, CodeBuild builds the
  first images, so nothing has to be pushed by hand.

### 2. Create the GitHub connection

Console → **CodePipeline → Settings → Connections** →
<https://console.aws.amazon.com/codesuite/settings/connections>

**Create connection** → GitHub → name it → *Connect to GitHub* → **Install a
new app** → authorize *AWS Connector for GitHub* → pick the repo → **Connect**.
Copy the ARN.

### 3. Create the secrets

CloudFormation **cannot** create `SecureString` parameters, so these three are
made by hand. Standard parameters are free; Secrets Manager would be $0.40 each
per month.

```bash
export AWS_REGION=us-east-1
export PROJECT=skill-upgradation-free

DB_PASSWORD=$(python -c "import secrets; print(secrets.token_urlsafe(24))")
SECRET_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(48))")

aws ssm put-parameter --region "$AWS_REGION" --name "/$PROJECT/DB_PASSWORD" \
  --type SecureString --value "$DB_PASSWORD"
aws ssm put-parameter --region "$AWS_REGION" --name "/$PROJECT/SECRET_KEY" \
  --type SecureString --value "$SECRET_KEY"
# Optional. Empty means the API uses its built-in offline analyzer.
aws ssm put-parameter --region "$AWS_REGION" --name "/$PROJECT/OPENAI_API_KEY" \
  --type SecureString --value ""

```

### 4. Platform stack — ~12 min

RDS reads its master password straight from `/$PROJECT/DB_PASSWORD` through a
`{{resolve:ssm-secure:...}}` dynamic reference, so there is no password
parameter to pass and nothing to keep in sync. Parameter Store is the single
source of truth.

```bash
aws cloudformation deploy \
  --region "$AWS_REGION" \
  --stack-name "$PROJECT-platform" \
  --template-file infra/free-tier/cloudformation/10-platform.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
      ProjectName="$PROJECT" \
      AllowedHttpCidr="0.0.0.0/0"
```

> Lock `AllowedHttpCidr` to `<your-ip>/32` while testing. This serves **plain
> HTTP** — a login password crosses the network in the clear.

<details>
<summary>Stacks created before this change: MySQL 1045 “Access denied”</summary>

Earlier versions took a `DbPassword` stack parameter that you *also* stored in
SSM by hand. The two copies drifted the moment either side changed alone, and
the symptom was a bare `1045 Access denied` at runtime — long after the deploy
reported success. Reset RDS to match Parameter Store:

```bash
PROJECT_NAME=skill-upgradation-free AWS_REGION=us-east-1 ./infra/free-tier/scripts/resync-db-password.sh --rotate
```

Run it from your machine, not the instance — the instance role has no
`rds:ModifyDBInstance`, by design. It sets Parameter Store and RDS from one
value, waits for the instance, then redeploys so `.env.runtime` is rewritten and
migrations run.

Resetting only the RDS password is the usual half-fix: `/opt/app/.env.runtime`
still holds the older value, so the API keeps failing and it looks like the
reset did not take.
</details>

Get the address:

```bash
aws cloudformation describe-stacks --region "$AWS_REGION" \
  --stack-name "$PROJECT-platform" \
  --query 'Stacks[0].Outputs' --output table
```

### 5. Pipeline stack — ~2 min

```bash
aws cloudformation deploy \
  --region "$AWS_REGION" \
  --stack-name "$PROJECT-pipeline" \
  --template-file infra/free-tier/cloudformation/20-pipeline.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
      ProjectName="$PROJECT" \
      GitHubOwner="your-github-username" \
      GitHubRepo="Skill-Upgradation" \
      GitHubBranch="main" \
      CodeStarConnectionArn="arn:aws:codeconnections:..." \
      PublicOrigin="http://<the-elastic-ip>"
```

### 6. Run it

The pipeline starts on its own once created, or:

```bash
aws codepipeline start-pipeline-execution --region "$AWS_REGION" \
  --name "$PROJECT-pipeline"
```

First run ~8 min: tests → two image builds → push → SSM deploy. Then open
`http://<elastic-ip>` and register — **the first account becomes admin**.

```bash
curl -X POST http://<elastic-ip>/api/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"a-strong-password","full_name":"Your Name"}'
```

## Console pages you'll use

| Page | Link |
|---|---|
| Free tier usage | <https://console.aws.amazon.com/billing/home#/freetier> |
| Budgets | <https://console.aws.amazon.com/costmanagement/home#/budgets> |
| CloudFormation stacks | <https://console.aws.amazon.com/cloudformation/home#/stacks> |
| CodePipeline | <https://console.aws.amazon.com/codesuite/codepipeline/pipelines> |
| CodeBuild history | <https://console.aws.amazon.com/codesuite/codebuild/projects> |
| EC2 instances | <https://console.aws.amazon.com/ec2/home#Instances:> |
| RDS databases | <https://console.aws.amazon.com/rds/home#databases:> |
| ECR repositories | <https://console.aws.amazon.com/ecr/repositories> |
| Parameter Store | <https://console.aws.amazon.com/systems-manager/parameters> |
| Session Manager (shell) | <https://console.aws.amazon.com/systems-manager/session-manager/sessions> |

## Operating it

**Shell on the box** — no SSH key, no open port, free:

```bash
aws ssm start-session --target <instance-id>
# then
sudo docker compose --env-file /opt/app/.env.runtime \
  -f /opt/app/docker-compose.prod.yml ps
sudo docker compose --env-file /opt/app/.env.runtime \
  -f /opt/app/docker-compose.prod.yml logs -f api
```

**Add an OpenAI key later:**

```bash
aws ssm put-parameter --region "$AWS_REGION" --name "/$PROJECT/OPENAI_API_KEY" \
  --type SecureString --value "sk-..." --overwrite
```

Then re-run the pipeline, or `sudo /opt/app/deploy.sh` on the instance.

**Redeploy without a code change:** re-run the pipeline, or on the instance
`sudo /opt/app/deploy.sh` — it re-reads Parameter Store and restarts.

**Stop paying while idle** (stops EC2 and RDS; the Elastic IP then starts
billing at ~$0.005/hr, so release it too for a long pause):

```bash
aws ec2 stop-instances --instance-ids <instance-id>
aws rds stop-db-instance --db-instance-identifier "$PROJECT-mysql"
```

RDS auto-restarts after 7 days — that is an AWS limit, not a setting.

## Known limits of this design

These are consequences of the free-tier constraint, stated plainly rather than
hidden:

- **HTTP only.** No ALB means no easy ACM certificate. For TLS, put CloudFront
  in front (free tier: 1 TB/mo out) or run certbot on the instance with a real
  domain.
- **One instance = downtime on deploy.** `docker compose up -d` restarts
  containers in place; expect a few seconds of 502. The Fargate design does a
  rolling update with no drop.
- **No auto-recovery.** If the instance dies, someone has to notice. No ASG, no
  health-check replacement.
- **Documents are on local EBS**, not S3 (`STORAGE_BACKEND=local`). Correct for
  one instance, wrong the moment there are two.
- **1 GB RAM.** A 2 GB swapfile is created at launch and the containers have
  memory caps, but a heavy AI pipeline run plus an image pull will be slow.
- **100 CodeBuild minutes/month** ≈ 15 builds. Past that it bills at about
  $0.005/min — pennies, but not zero.

When any of these stops being acceptable, [the Fargate
stacks](../cloudformation/) are the same application with none of these
compromises.
