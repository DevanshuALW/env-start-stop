terraform {
  required_version = "~> 1.14.0"
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 6.28.0" }
    archive = { source = "hashicorp/archive", version = "~> 2.7.0" }
  }
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = {
      ManagedBy   = "terraform"
      Environment = var.environment
      Application = var.name_prefix
      Client      = var.client_name
    }
  }
}
