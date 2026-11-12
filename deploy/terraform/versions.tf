terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source = "hashicorp/aws"
      # 6.x: aws_lambda_permission.invoked_via_function_url, needed by
      # Function URLs with auth type NONE.
      version = "~> 6.50"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = "oe-agent"
      ManagedBy = "terraform"
    }
  }
}
