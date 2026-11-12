variable "region" {
  description = "AWS region for every resource."
  type        = string
  default     = "us-east-1"
}

variable "name" {
  description = "Name of the function, ECR repository and log group. deploy-policy.json names the same resources: change both together."
  type        = string
  default     = "oe-agent"
}

variable "image_tag" {
  description = "Tag of the image pushed to ECR (the short git commit, by convention). Empty only for the first, ECR-only apply."
  type        = string
  default     = ""
}

variable "bedrock_model_id" {
  description = "Bedrock inference-profile id the agent calls. Confirm it with `aws bedrock list-inference-profiles` before applying."
  type        = string
  # The profile that answered on 12 Nov 2026 (deploy/README.md, the run record).
  default     = "us.anthropic.claude-sonnet-4-6"

  validation {
    condition     = startswith(var.bedrock_model_id, "us.")
    error_message = "Expected a US cross-region inference-profile id (us.<model>)."
  }
}

variable "bedrock_regions" {
  description = "Regions the us. inference profile may route requests to; the role may invoke the foundation model only there."
  type        = list(string)
  default     = ["us-east-1", "us-east-2", "us-west-2"]
}

variable "key_parameter_name" {
  description = "SSM Parameter Store SecureString holding the access key. Created by hand (deploy/README.md), never by Terraform, so the key is not in Terraform code or state."
  type        = string
  default     = "/oe-agent/service-key"

  validation {
    condition     = startswith(var.key_parameter_name, "/")
    error_message = "Use a path-style name that starts with /."
  }
}

variable "reserved_concurrency" {
  description = "Most copies of the function that can run at once. -1 removes the cap (needed only if the account's concurrency limit is 10; see deploy/README.md)."
  type        = number
  default     = 2
}

variable "timeout_seconds" {
  description = "Function timeout: room for the agent's bounded loop (9 model calls at the default max_steps of 6; the API allows up to 12)."
  type        = number
  default     = 300
}

variable "memory_mb" {
  description = "Function memory (also sets its CPU share)."
  type        = number
  default     = 1024
}

variable "monthly_budget_usd" {
  description = "Monthly cost budget for the whole account; alerts go to alert_email."
  type        = number
  default     = 5
}

variable "alert_email" {
  description = "Where budget alerts go. Set it in terraform.tfvars (untracked), not here."
  type        = string
}
