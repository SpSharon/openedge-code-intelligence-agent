output "function_url" {
  description = "The service's public address (the access key is still required)."
  value       = aws_lambda_function_url.service.function_url
}

output "ecr_repository_url" {
  value = aws_ecr_repository.service.repository_url
}

output "function_name" {
  value = aws_lambda_function.service.function_name
}

output "log_group" {
  value = aws_cloudwatch_log_group.service.name
}
