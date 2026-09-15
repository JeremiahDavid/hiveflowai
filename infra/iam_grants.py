"""Shared IAM grant helpers for Glue catalog sync, Athena, and Bedrock access.

Plain functions, not a construct — they only attach policies to an existing
principal and create no new resources, so keeping them out of the stack
classes has zero CloudFormation impact (used by IngestStack, DnaStack, and
GlobalAgentPipelinesStack).
"""

from __future__ import annotations

from aws_cdk import Stack, aws_iam as iam, aws_lambda as _lambda


def _attach_policy(
    principal: iam.IRole | _lambda.Function,
    policy: iam.PolicyStatement,
) -> None:
    if isinstance(principal, _lambda.Function):
        principal.add_to_role_policy(policy)
    else:
        principal.add_to_policy(policy)


def grant_glue_catalog_sync(
    principal: iam.IRole | _lambda.Function,
    *,
    company: str,
    environment: str,
) -> None:
    from hiveflow.project_config import glue_database_name

    stack = Stack.of(principal)
    database_name = glue_database_name(company, environment)
    _attach_policy(
        principal,
        iam.PolicyStatement(
            actions=[
                "glue:CreateTable",
                "glue:DeleteTable",
                "glue:GetTable",
                "glue:UpdateTable",
            ],
            resources=[
                f"arn:aws:glue:{stack.region}:{stack.account}:catalog",
                f"arn:aws:glue:{stack.region}:{stack.account}:database/{database_name}",
                f"arn:aws:glue:{stack.region}:{stack.account}:table/{database_name}/*",
            ],
        ),
    )


def grant_athena_query(
    principal: iam.IRole | _lambda.Function,
    *,
    company: str,
    environment: str,
) -> None:
    """Allow deterministic SQL pack replay / validation against the company workgroup."""
    from hiveflow.project_config import (
        glue_database_name,
        resolve_athena_results_bucket_name,
    )

    stack = Stack.of(principal)
    database_name = glue_database_name(company, environment)
    results_bucket = resolve_athena_results_bucket_name(
        company,
        environment,
        account=stack.account,
        region=stack.region,
    )
    _attach_policy(
        principal,
        iam.PolicyStatement(
            actions=[
                "athena:StartQueryExecution",
                "athena:GetQueryExecution",
                "athena:GetQueryResults",
                "athena:StopQueryExecution",
                "athena:GetWorkGroup",
            ],
            resources=["*"],
        ),
    )
    _attach_policy(
        principal,
        iam.PolicyStatement(
            actions=[
                "glue:GetDatabase",
                "glue:GetDatabases",
                "glue:GetTable",
                "glue:GetTables",
                "glue:GetPartition",
                "glue:GetPartitions",
            ],
            resources=[
                f"arn:aws:glue:{stack.region}:{stack.account}:catalog",
                f"arn:aws:glue:{stack.region}:{stack.account}:database/{database_name}",
                f"arn:aws:glue:{stack.region}:{stack.account}:table/{database_name}/*",
            ],
        ),
    )
    _attach_policy(
        principal,
        iam.PolicyStatement(
            actions=[
                "s3:GetBucketLocation",
                "s3:GetObject",
                "s3:ListBucket",
                "s3:PutObject",
                "s3:DeleteObject",
            ],
            resources=[
                f"arn:aws:s3:::{results_bucket}",
                f"arn:aws:s3:::{results_bucket}/*",
            ],
        ),
    )


def grant_bedrock_semantic_access(principal: iam.IRole | _lambda.Function) -> None:
    """Semantic init LLM column tagging + Titan embeddings for doc retrieval."""
    _attach_policy(
        principal,
        iam.PolicyStatement(
            actions=[
                "bedrock:InvokeModel",
                "bedrock:InvokeModelWithResponseStream",
                "bedrock:Converse",
                "bedrock:ConverseStream",
                "aws-marketplace:ViewSubscriptions",
                "aws-marketplace:Subscribe",
                "aws-marketplace:Unsubscribe",
            ],
            resources=["*"],
        ),
    )
