# `wreath.serverless`

Warm non-AWS adapters for platforms that do not already accept an ASGI
callable. Google Functions receives a translator; Azure Functions is handed to
its native `AsgiFunctionApp`, while Vercel accepts the Wreath `app` directly.
AWS API Gateway and Function URL deployments use the single canonical import,
`wreath.aws_lambda.LambdaAdapter`.

::: wreath.serverless
