# Deploying the service to AWS

The FastAPI service (`service/`) runs as a **Docker container on AWS Lambda**, reached through a **Function URL**, calling Claude through **Amazon Bedrock**, behind an **access key**. Terraform describes everything except the key itself.

| Piece | What it is |
|---|---|
| `Dockerfile` | The image: Python 3.11, the service, the index (built at image build time), and the AWS Lambda Web Adapter, which lets uvicorn run unchanged on Lambda. |
| `Dockerfile.dockerignore` | An allow-list: only the code and corpus reach the image. |
| `requirements-lambda.txt` | boto3 (Bedrock, and reading the key from SSM). |
| `terraform/` | ECR repository (keeps the last 3 images), Lambda function (reserved concurrency 2, 300 s timeout), Function URL (streaming), IAM role (one model, one parameter, one log group), CloudWatch log group (14 days), and a $5/month budget alarm. |
| `deploy-policy.json` | The permissions of the identity that deploys: a separate IAM user, `deploy-dev`, that holds this policy and nothing else. It can create and remove this one service and read its logs. The policy gives it no permission to call the model or to read the key itself; it does control the function's role and code, so it is a narrow deploy identity, not a hard boundary (no permissions boundary is set on the role). The user that calls the model from a laptop (`bedrock-dev`) was not used to deploy. |

**What it costs while idle:** close to nothing. Lambda bills per request, ECR storage for 3 images is cents, and the parameter is free. Each answered question costs the Bedrock tokens, as on your laptop. The budget **emails**; it does not stop anything.

**The access key** lives in SSM Parameter Store as an encrypted SecureString. You create it with one command; Terraform refers to it only by name, so the key never appears in the code, the Terraform files or the Terraform state. The function reads it once at start-up. Every `/ask` and `/ask/stream` call must send it as the `X-API-Key` header; without it the answer is 401 and the model is never called. `/health` and `/docs` stay open.

All commands are PowerShell, run from the repository root with the virtual environment active (`.venv\Scripts\Activate.ps1`), and Docker Desktop running.

---

## 1 · Local check (no AWS)

```powershell
python -m unittest discover -s tests          # Ran 263 tests ... OK (with every optional package installed)
docker build --platform linux/amd64 --provenance=false -f deploy/Dockerfile -t oe-agent:local .
$env:OE_SERVICE_KEY = "local-test-key-0123456789"
docker run --rm -p 8000:8000 -e OE_AGENT_BACKEND=fake -e OE_SERVICE_KEY oe-agent:local
```

In a **second** PowerShell window (same folder, venv active):

```powershell
$env:OE_SERVICE_KEY = "local-test-key-0123456789"
python -m service.cli health                                   # "key_configured": true
python -m service.cli ask "what calls ar-invoice.p?"           # a reply from the fake backend: (refused: not in the retrieved code)
python -m service.cli --key wrong-key-0123456789 ask "what calls ar-invoice.p?"   # error 401
```

Open <http://127.0.0.1:8000/docs>: **Authorize** → paste `local-test-key-0123456789` → try `/ask`. Stop the container with Ctrl+C in the first window.

## 2 · Create the deploying user (once)

Make your copy of the policy with your account number filled in (the copy is untracked; the first line uses your existing default profile — or copy the account number from the console):

```powershell
$acct = aws sts get-caller-identity --query Account --output text
(Get-Content deploy\deploy-policy.json -Raw) -replace 'ACCOUNT_ID', $acct | Set-Content deploy\deploy-policy.local.json
```

Then, signed in to the AWS console yourself as your administrator — the one administrative action on the normal path:

1. **IAM → Policies → Create policy → JSON**: paste the contents of `deploy\deploy-policy.local.json`; name it `oe-agent-deploy`.
2. **IAM → Users → Create user**: name `deploy-dev`, no console access; **Attach policies directly → oe-agent-deploy**, and nothing else.
3. **Users → deploy-dev → Security credentials → Create access key** (use case: command line interface). Keep the page open.

Save the key as a named profile — the command prompts for the two values, so they never appear on a command line — then sign out of the console:

```powershell
aws configure --profile deploy-dev        # the two values, then us-east-1, then json
$env:AWS_PROFILE = "deploy-dev"           # every AWS and Terraform command in THIS window now runs as deploy-dev
aws sts get-caller-identity --query Arn --output text     # must end in user/deploy-dev
```

Do everything from here on in that one window: it holds the profile, after step 4 the access key, and after step 5 the image tag.

## 3 · Pre-flight checks

```powershell
aws bedrock list-inference-profiles --region us-east-1 --output text --query "inferenceProfileSummaries[?contains(inferenceProfileId, 'sonnet')].[inferenceProfileId, status]"
aws bedrock list-inference-profiles --region us-east-1 --output text --query "inferenceProfileSummaries[?inferenceProfileId=='us.anthropic.claude-sonnet-4-6'].models[].modelArn"
aws lambda get-account-settings --region us-east-1 --query AccountLimit
```

- The first command lists the profiles and their status. **`ACTIVE` is necessary, not sufficient**: on 12 Nov 2026 two `ACTIVE` profiles refused this account at the first question (see the run record at the end). The default in `terraform/variables.tf` is the profile that answered that day, `us.anthropic.claude-sonnet-4-6`; to use another, add `-var bedrock_model_id=<profile id>` to every `terraform plan` and `apply` below.
- The second prints the foundation-model addresses the profile routes to. The function's role is built from them: the name after `foundation-model/` must be the profile id without `us.`, and the regions must be the ones in `bedrock_regions` (`us-east-1`, `us-east-2`, `us-west-2`).
- In the third, look at `UnreservedConcurrentExecutions`. AWS keeps part of the account limit unreserved, so a cap of 2 cannot be counted on when it shows 10 (the case on 12 Nov 2026; the cap was not attempted and the exact threshold was not tested). If it shows 10, either ask for a higher limit (**Service Quotas → AWS Lambda → Concurrent executions**; free, takes a few days), or deploy now without the cap by adding `-var reserved_concurrency=-1` to every `terraform plan` and `apply` below. Without the cap, the key still keeps strangers from reaching the model.

## 4 · Create the access key

```powershell
$k = python -c "import secrets; print(secrets.token_hex(32))"
aws ssm put-parameter --region us-east-1 --name /oe-agent/service-key --type SecureString --value $k
$k | Set-Clipboard
```

Paste it into your password manager **now**. The history keeps the command, not the key. `deploy-dev` has no permission to read the key back or to list parameters (on purpose; `describe-parameters` was refused on 12 Nov 2026): if you lose it, rotate it (see *Updating the service later*). Use `token_hex`, not other formats: a key that starts with `-` would be read by the AWS CLI as an option.

## 5 · Create the image repository, then push the image

```powershell
Copy-Item deploy\terraform\terraform.tfvars.example deploy\terraform\terraform.tfvars
notepad deploy\terraform\terraform.tfvars        # your email for the budget alerts
terraform -chdir=deploy/terraform init
terraform -chdir=deploy/terraform apply '-target=aws_ecr_repository.service' '-target=aws_ecr_lifecycle_policy.service'
```

Terraform warns that `-target` is for exceptional use. This is that case: the repository has to exist before there is an image to put in it.

```powershell
$repo = terraform -chdir=deploy/terraform output -raw ecr_repository_url
$tag  = "$(git rev-parse --short HEAD)-$(Get-Date -Format yyyyMMddHHmm)"
$registry = $repo.Split('/')[0]
cmd /c "aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin $registry"
docker tag oe-agent:local "${repo}:$tag"
docker push "${repo}:$tag"
```

The login goes through `cmd /c` because the same pipe run directly in Windows PowerShell 5.1 failed with `400 Bad Request` on 12 Nov 2026 (PowerShell re-encodes text piped between two programs); through `cmd /c` it succeeded.

## 6 · Deploy everything else

```powershell
terraform -chdir=deploy/terraform plan  -var image_tag=$tag
terraform -chdir=deploy/terraform apply -var image_tag=$tag
```

Read the plan before you type `yes`. It should list the IAM role and its policy, the log group, the function, the Function URL, two permissions and the budget, and **nothing destroyed**.

If `apply` stops with `AccessDenied` naming an action, add that action to the matching statement of `oe-agent-deploy` in the console and run `apply` again. Note what you added: it belongs in `deploy-policy.json` too. (On 12 Nov 2026 nothing had to be added.)

## 7 · Smoke test

In the same window as step 4 (otherwise set `$k` by pasting the key from your password manager: `$k = Read-Host`):

```powershell
$url = terraform -chdir=deploy/terraform output -raw function_url
$env:OE_SERVICE_KEY = $k
python -m service.cli --url $url health                                  # "status": "ok", "backend": "bedrock", "key_configured": true
python -m service.cli --url $url ask "what calls ar-invoice.p?"          # a real answer, with citations and a cost line
python -m service.cli --url $url --key wrong-key-0123456789abcdef ask "what calls ar-invoice.p?"   # error 401
```

Then open `$url` + `docs` in your browser (`Start-Process "${url}docs"`). Click **Authorize**, paste the key, and try `/ask/stream` (the browser check and `/ask/stream` were not exercised on Lambda in the 12 Nov 2026 run).

The first call after a quiet period takes a few seconds longer: that is a Lambda cold start. To see the logs:

```powershell
aws logs tail /aws/lambda/oe-agent --region us-east-1 --since 15m
```

If `ask` answers `error 500`, the cause is in the log. On 12 Nov 2026 the traceback appeared there only after a later request woke the function (Lambda freezes it between requests), so call `health` once, then read the log; the line that named the cause started with `botocore.`:

```powershell
python -m service.cli --url $url health
aws logs tail /aws/lambda/oe-agent --region us-east-1 --since 10m | Select-String "botocore\.|REPORT" | Select-Object -Last 6
```

## 8 · Put the deploy key away

The deployed service keeps running without `deploy-dev`: it uses its own role. Close the window (the profile setting, the access key variable and the tag go with it). The `deploy-dev` key itself remains in `~\.aws\credentials` until it is deactivated or deleted. When no deployment or teardown is planned, the `deploy-dev` access key can be deactivated (**IAM → Users → deploy-dev → Security credentials** — an administrator's action in the console) and reactivated for the next one.

---

## Updating the service later

In a window with `$env:AWS_PROFILE = "deploy-dev"`: rebuild, then push under a new tag (step 1's `docker build`, then step 5's second block, which makes a fresh `$tag`) and apply, with the same extra `-var` values as the first deployment (step 3):

```powershell
terraform -chdir=deploy/terraform apply -var image_tag=$tag
```

**To rotate the key** (as `deploy-dev`): make a new `$k` as in step 4 and run the same `put-parameter` command with `--overwrite`. The new key takes effect at the function's next cold start; force one with `aws lambda update-function-configuration --function-name oe-agent --description "key rotated"`. The next `terraform apply` should clear that description again, which is harmless (rotation was not tested).

## Teardown

As `deploy-dev` (a new window needs `$env:AWS_PROFILE = "deploy-dev"` again):

```powershell
terraform -chdir=deploy/terraform destroy
```

Read the plan before you type `yes`: ten to destroy, nothing added or changed. (On 12 Nov 2026 it was run with the same `-var` values as the last `apply`.)

This removes the function, the Function URL, the role, the log group, the budget, and the repository **with its images** (ten resources). To retire the service for good, also delete the key:

```powershell
aws ssm delete-parameter --region us-east-1 --name /oe-agent/service-key
```

**Proving the teardown once:** run `destroy`, then repeat step 5 from the `-target` apply onwards (not the `Copy-Item`, which would overwrite your `terraform.tfvars`), then step 6. The repository is new, so the image is pushed again. Keep the key parameter for the round trip, so the key doesn't change. The service should come back at a **new** Function URL.

## Files that must never be committed

`.gitignore` covers `deploy/terraform/terraform.tfvars` (your email), `*.tfstate*` (Terraform's record of what exists), `.terraform/`, and `deploy/*.local.json` (the policy with your account number).

**Do commit `deploy/terraform/.terraform.lock.hcl`** after the first `init`. It pins the provider version you actually used.

**Keep `deploy/terraform/terraform.tfstate` safe.** It's how Terraform knows what to destroy.

---

## The run record — 12 Nov 2026

Sharon Paul ran steps 1–7 and the teardown of this document once (the exceptions are listed below), on her own machine and AWS account (Windows, Docker Desktop, Terraform 1.16.4 with the AWS provider 6.73.0, AWS CLI 2.37.9), as `deploy-dev`, and tore it down about twenty minutes after deploying. What happened, from the output, in the order it happened:

| Step | Result |
|---|---|
| Local check | Image built; against the container with the `fake` backend: `health` ok, a reply, `401` for a wrong key |
| Repository and image | `apply` with `-target`: 2 added. The registry login failed from PowerShell with `400 Bad Request` and succeeded through `cmd /c`. Image pushed; the digest pushed is the digest built |
| Deploy | `Plan: 8 to add, 0 to change, 0 to destroy`; applied without the concurrency cap (`-var reserved_concurrency=-1`: the account's limit is 10; the cap was not attempted) |
| `health` on Lambda | ok — backend `bedrock`, 35 units, key read from the parameter store |
| First question, Claude Sonnet 4.5 | `500`. The log: Bedrock refused — the model is marked Legacy and the account had not used it in 30 days |
| Second, Claude Sonnet 5.5 | `500`. The log: the model *"is not available for this account"* |
| Wrong key on Lambda | `401` (`missing or invalid X-API-Key`) |
| Third, Claude Sonnet 4.6 | Answered: the caller of `ar-invoice.p` with two citations; one model call, estimated $0.0183 |
| Measured by AWS | Cold start (`Init Duration`) 1.8–2.0 s; the answered question 2.5 s; 133 MB used of 1,024 MB |
| Teardown | `Destroy complete! Resources: 10 destroyed`; the key parameter deleted; `get-function` and `describe-repositories` answer not found; the old address answers 403 |

Each model change was one `apply` with a different `-var bedrock_model_id` (`0 to add, 2 to change`): the role's policy and one environment variable. No tracked file had to be edited to deploy; the default model id was changed afterwards to the one that answered, and this document was brought into line with what was done (the `deploy-dev` user, the login through `cmd /c`, reading the log after a 500).

What the run does not show: one deployment, three questions asked and one answered, about twenty minutes of life — no load, no streaming call through the Function URL (`/ask/stream` was not exercised on Lambda), no browser `/docs` check on Lambda, no key rotation, no update of a running service, no second deployment from the same files (the "proving the teardown" round trip above was not done). The `deploy-dev` user was created with the account's administrator sign-in; nothing else in the run used it (the account number for the policy was read with the existing default profile).
