# Env start/stop — setup guide

This folder is a **copy-and-fill** stack. One client = one copy of this folder + their own `terraform.tfvars`.

It does **not** change any other client's live automation. Do not apply this from `platform/non-prod/start-stop-automation`.

**AWS + Kubernetes only** (EKS, RDS instances, Lambda, EventBridge). Not GCP. Not Aurora clusters (those use a different API).

---

## What this does

Someone types a Slack slash command, or a nightly clock fires.

1. **STOP** — pause Argo parent → pause child apps → delete HPAs → scale deploy/STS to 0 → suspend CronJobs → stop RDS (no wait).
2. **START (RDS)** — start any listed DB that is `stopped`.
3. **START (apps)** — wait until those DBs are `available` → unpause apps → unsuspend CronJobs → unpause parent → scale leftover 0-replica workloads to HPA min (or 1).

Slack posts start / done / failed in the channel you set.

---

## What you need before day 1

Collect these. If any are missing, stop and get them first.

- [ ] Non-prod AWS account id (never prod)
- [ ] AWS CLI profile that can create Lambda, IAM, Secrets Manager, EventBridge Scheduler
- [ ] EKS cluster name(s) + region(s)
- [ ] `kubectl` context(s) for those clusters
- [ ] Argo CD namespace (usually `argocd`)
- [ ] For each floor: Kubernetes namespace, Argo parent app name, child Argo app names, RDS instance id (or none)
- [ ] Slack workspace where you can create an app
- [ ] Slack channel id for results (open the channel → copy link → the `C…` id)
- [ ] Nightly timezone (example `Asia/Calcutta`) and whether they want the clock at all
- [ ] EKS API reachable from Lambda (public endpoint, or you must put Lambda in a VPC yourself — this stack does not add VPC)

`environment = "prod"` / `production` is rejected on purpose. Slack `modal_title` max **24 characters**.

---

## Step 0 — Copy the folder

Do this on your laptop. One folder per client.

```bash
cp -R templates/env-start-stop ~/clients/acme-env-start-stop
cd ~/clients/acme-env-start-stop
cp terraform.tfvars.example terraform.tfvars
```

Optional remote state (recommended):

```bash
cp backend.tf.example backend.tf
```

Edit `backend.tf`: this client's bucket and a **unique** key, for example `clients/acme/env-start-stop/terraform.tfstate`. Never reuse another client's key.

---

## Step 1 — Fill `terraform.tfvars`

Open `terraform.tfvars`. Replace every example value.

| Field | What to put |
|---|---|
| `client_name` | Short label for tags only (`acme`) |
| `name_prefix` | Unique in the account (`acme-np-start-stop`). Becomes Lambda / IAM / secret names |
| `aws_region` | Region where the Lambda and schedules live |
| `aws_account_id` | That non-prod account |
| `slack_channel_id` | `C…` channel id |
| `slack_commands` | Slash command(s), example `["/env-start-stop"]` |
| `modal_title` | Title on the Slack form (**max 24 characters**) |
| `argocd_namespace` | Usually `argocd` |
| `rbac_group` | Leave `start-stop-automation` unless the client already uses another group |
| `schedule_timezone` | IANA name, example `Asia/Calcutta` |
| `regions` | One block per EKS cluster |
| `targets` | One Slack row per namespace |
| `defaults` | Which rows are pre-ticked when a region is picked |
| `schedules` | Nightly jobs, or `schedules = {}` for Slack only |

**`regions` example**

```hcl
{
  key        = "use1"                    # Slack value, no spaces
  label      = "Virginia (us-east-1)"    # dropdown text
  short_name = "Virginia"                # chip: "Virginia · dev-app"
  aws_region = "us-east-1"
  cluster    = "acme-nonprod-eks"
}
```

**`targets` example**

```hcl
{
  key        = "use1:dev-app"   # must be region_key:something
  region_key = "use1"           # must match a regions.key
  namespace  = "dev-app"        # Kubernetes namespace
  parent     = "dev-root-app"   # Argo app-of-apps. Use "" if none
  rds        = ["dev-app"]      # RDS instance ids. Use [] if no DB
  apps       = ["dev-api", "dev-worker"]  # Argo child Application names
}
```

Rules:

- Do not list an RDS id that does not exist in that region. Instance RDS only, not Aurora cluster ids.
- Add the Role/RoleBinding in that namespace **before** you put it in `targets` and run nightly STOP.
- Do not commit `terraform.tfvars` if it has client secrets. Channel id is fine to keep local.

---

## Step 2 — Create the Slack app (Request URL later)

In [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → from scratch → the client's workspace.

1. **OAuth & Permissions** → Bot Token Scopes:
   - `commands`
   - `chat:write`
   - `users:read`
2. **Install to Workspace**. Copy the **Bot User OAuth Token** (`xoxb-…`). Do not paste it into git or Slack.
3. **Basic Information** → **Signing Secret**. Copy it.
4. **Slash Commands** → Create:
   - Command: same as `slack_commands` (example `/env-start-stop`)
   - Request URL: put `https://example.com/placeholder` for now. You will replace it in step 5.
5. **Interactivity & Shortcuts** → On. Same Request URL as the slash command (placeholder is fine until step 5).
6. Invite the bot to the results channel.

Keep the two secret values in your password manager. You need them in step 4.

---

## Step 3 — Confirm the AWS account

```bash
export AWS_PROFILE=client-nonprod
aws sts get-caller-identity
```

Account must match `aws_account_id`. If this is a prod account, stop.

---

## Step 4 — Create empty Slack secrets, then put values in

Empty secrets + Lambda pointed at them = Slack dies. Do this in two moves.

```bash
cd ~/clients/acme-env-start-stop
terraform init
terraform apply \
  -target=aws_secretsmanager_secret.slack_signing_secret \
  -target=aws_secretsmanager_secret.slack_bot_token
```

Type `yes`. You should see **2 to add**.

```bash
# name_prefix from your tfvars
PREFIX=acme-np-start-stop

aws secretsmanager put-secret-value \
  --secret-id "${PREFIX}/slack-signing-secret" \
  --secret-string 'PASTE_SIGNING_SECRET'

aws secretsmanager put-secret-value \
  --secret-id "${PREFIX}/slack-bot-token" \
  --secret-string 'PASTE_BOT_TOKEN'

aws secretsmanager list-secret-version-ids --secret-id "${PREFIX}/slack-signing-secret"
aws secretsmanager list-secret-version-ids --secret-id "${PREFIX}/slack-bot-token"
```

Both must show a version with `AWSCURRENT`. Only then continue.

---

## Step 5 — Apply the rest

```bash
terraform plan -out=tfplan
```

Read the plan. You want **creates** (Lambda, IAM, Function URL, schedules). **0 destroy**. No other client's names.

```bash
terraform apply tfplan
terraform output
```

Copy:

- `function_url`
- `lambda_role_arn`
- `slack_signing_secret_name` / `slack_bot_token_secret_name` (already filled)

Back in Slack:

1. Slash command Request URL = `function_url`
2. Interactivity Request URL = same `function_url`
3. Save

Do not change the Function URL later without updating Slack.

---

## Step 6 — Let the Lambda into each EKS cluster

The Lambda role needs a Kubernetes group that matches `rbac_group` (default `start-stop-automation`).

**If the cluster uses EKS access entries:**

```bash
chmod +x scripts/*.sh
./scripts/grant-eks-access.sh \
  acme-nonprod-eks \
  us-east-1 \
  arn:aws:iam::123456789012:role/acme-np-start-stop-lambda \
  start-stop-automation
```

Use the real cluster, region, and `lambda_role_arn` from `terraform output`. Repeat once per cluster in `regions`.

**If the cluster still uses `aws-auth`:**

```yaml
# mapRoles entry
- rolearn: arn:aws:iam::123456789012:role/acme-np-start-stop-lambda
  username: env-start-stop
  groups:
    - start-stop-automation
```

Do not add `AmazonEKSClusterAdminPolicy` unless you have no namespace Roles. This stack is meant to use the group + per-namespace Roles only.

---

## Step 7 — Kubernetes keycards (RBAC)

Without this, STOP/START returns **403** on HPAs or deployments.

On **each** cluster, apply `argocd` plus every namespace in `targets`. After apply, Terraform prints the list:

```bash
terraform output -json rbac_namespaces_by_cluster
```

```bash
export KUBE_CONTEXT=arn:aws:eks:us-east-1:123456789012:cluster/acme-nonprod-eks
export RBAC_GROUP=start-stop-automation
export ARGOCD_NAMESPACE=argocd

./scripts/apply-rbac.sh argocd dev-app uat-app
```

Check:

```bash
kubectl --context "$KUBE_CONTEXT" get role,rolebinding env-start-stop -n argocd
kubectl --context "$KUBE_CONTEXT" get role,rolebinding env-start-stop -n dev-app
kubectl --context "$KUBE_CONTEXT" get role,rolebinding env-start-stop -n uat-app
```

New namespace later = add Role/RoleBinding **first**, then add the target and `terraform apply`.

---

## Step 8 — Prove it (one unused floor)

Do not test on a shared namespace the first time.

1. In Slack, run the slash command. The form must open.
2. Pick **Stop**, one region, **one** unused namespace, reason `rbac test`.
3. Wait for **STOP done** in the channel.
4. Check apps and RDS:

```bash
kubectl --context "$KUBE_CONTEXT" get deploy,sts -n dev-app
aws rds describe-db-instances --db-instance-identifier dev-app \
  --query 'DBInstances[0].DBInstanceStatus'
```

Deploys should be `0/0`. RDS should be `stopping` or `stopped` if you listed one.

5. Same form → **Start** → same namespace.
6. Wait for **START done**. Deploys should leave `0/0`. RDS should be `available`.

If Slack says `Unauthorized`, the signing secret is wrong. If the form does not open, check Interactivity URL and bot scopes. If you get a k8s **403**, step 6 or 7 is incomplete.

---

## Step 9 — Turn on the clock (if you used `schedules`)

Default tfvars:

| Job | Cron (in `schedule_timezone`) | Action |
|---|---|---|
| `stop` | 23:00 | STOP all targets |
| `start_rds` | 05:40 | START RDS only |
| `start_apps` | 06:00 | START apps (waits for RDS) |

Confirm:

```bash
terraform output schedule_names
```

Leave them enabled only after step 8 passed.

To disable the clock without deleting the stack, set `schedules = {}` and apply.

---

## Day to day

| You want | You do |
|---|---|
| New Slack row | RBAC on that namespace → add `targets` row → `terraform apply` |
| New RDS on an existing row | Add the id to `rds` (it must exist) → apply |
| New cluster | Add `regions` → apply → step 6 + 7 on that cluster |
| New time / timezone | Edit `schedules` / `schedule_timezone` → apply |
| New Slack channel | Change `slack_channel_id` → apply. Invite the bot |
| Rotate Slack tokens | `put-secret-value` on the same two secret names. No apply needed |

`terraform apply` rebuilds the zip from `lambda_function.py` + a generated `config.json`. You do not edit the Python for client names.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Slash command `Unauthorized` | Signing secret empty or wrong |
| Form does not open | Request URL / Interactivity URL not the Function URL |
| `403` list HPAs | Missing Role/RoleBinding in that namespace |
| `403` Argo Application | Missing Role in `argocd` |
| START times out on RDS | Listed a DB that does not exist, Aurora cluster id, or IAM ARN mismatch |
| Nightly FAILED with one floor in the error | Other floors in that region were still processed; fix RBAC on the named one |
| Lambda cannot reach EKS | Cluster endpoint is private-only; Lambda is not in that VPC |
| Slack posts nothing | Bot not in the channel, or `chat:write` missing |

---

## Guardrails already in this stack

- `environment` cannot be prod / production / prd.
- Duplicate region or target keys fail plan.
- `targets.region_key` and `defaults` must point at real keys.
- Slack form: max 100 namespaces per region; modal title max 24 chars.
- One namespace 403 no longer aborts the rest of that region’s STOP/START. Slack still reports FAILED with the floors that broke.
- Missing RDS is skipped on wait (do not list ids that do not exist).
- RDS `InvalidDBInstanceState` (already stopping/starting) is skipped, not a crash.
- Slack secrets are read on first use, so a worker can run if you only invoked the scheduler path after secrets exist.
- CloudWatch logs retain `log_retention_days` (default 30).

## Do not

- Use a prod account or `environment = "prod"`.
- Reuse `name_prefix` or a state key from another client.
- Apply this folder onto an already-named live stack (different resource names will fight).
- Add a target before its namespace Role exists.
- Commit Slack tokens or `terraform.tfvars` with secrets.
- Point Lambda at a private-only EKS API without putting Lambda in that VPC (not in this template).

---

## Folder map

| Path | Role |
|---|---|
| `lambda_function.py` | Shared brain. Reads `config.json` |
| `main.tf` | Lambda, URL, IAM, secrets, schedules |
| `variables.tf` / `terraform.tfvars` | Client knobs |
| `provider.tf` / `backend.tf` | AWS region + optional S3 state |
| `rbac/` | Role YAML for app namespaces and Argo |
| `scripts/grant-eks-access.sh` | Map Lambda role → k8s group |
| `scripts/apply-rbac.sh` | Apply those Roles |
