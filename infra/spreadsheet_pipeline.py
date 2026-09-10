from __future__ import annotations

import os
from typing import Any

from aws_cdk import Duration
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from constructs import Construct

from lambda_bundle import PROJECT_ROOT, HiveFlowLambdaRuntime
from hiveflow.process_config import Process, lambda_name_for_process, step_function_name_for_process

# Bump to force the interpret/propose container images to rebuild + redeploy.
AGENT_IMAGE_REVISION = "20260910-agent-sdk-interpret"

# Default Bedrock model for the Agent SDK path (converse fallback keeps
# HIVEFLOW_BEDROCK_MODEL_ID). Overridable via the HIVEFLOW_AGENT_MODEL /
# HIVEFLOW_AGENT_MAX_BUDGET_USD env vars at synth time.
_AGENT_MODEL = os.getenv("HIVEFLOW_AGENT_MODEL", "us.anthropic.claude-sonnet-5")
_AGENT_MAX_BUDGET_USD = os.getenv("HIVEFLOW_AGENT_MAX_BUDGET_USD", "1.00")


def _agent_image_code(handler: str) -> _lambda.DockerImageCode:
    """Container image for an Agent-SDK spreadsheet stage (Node + `claude` CLI)."""
    return _lambda.DockerImageCode.from_image_asset(
        str(PROJECT_ROOT),
        file="infra/agent_image/Dockerfile",
        cmd=[handler],
        platform=ecr_assets.Platform.LINUX_AMD64,
        build_args={"AGENT_IMAGE_REVISION": AGENT_IMAGE_REVISION},
    )


def _apply_lambda_throttle_retry(task: tasks.LambdaInvoke) -> tasks.LambdaInvoke:
    task.add_retry(
        errors=["Lambda.TooManyRequestsException"],
        interval=Duration.seconds(5),
        max_attempts=8,
        backoff_rate=2,
    )
    return task


def create_spreadsheet_pipeline(
    scope: Construct,
    construct_id: str,
    *,
    company: str,
    environment: str,
    data_bucket: s3.IBucket,
    lambda_runtime: HiveFlowLambdaRuntime,
    common_env: dict[str, str],
    grant_bedrock: Any,
) -> dict[str, Any]:
    """Spreadsheet Engine: parse -> profile -> interpret -> propose."""
    prefix = construct_id

    parse_fn = _lambda.Function(
        scope,
        f"{prefix}SpreadsheetParseFunction",
        function_name=lambda_name_for_process(
            company, environment, "all", Process.SPREADSHEET_PARSE
        ),
        runtime=_lambda.Runtime.PYTHON_3_12,
        handler="hiveflow.spreadsheet.handlers.parse_handler",
        timeout=Duration.minutes(5),
        memory_size=1024,
        description="Spreadsheet Engine: parse uploaded Excel workbooks",
        code=lambda_runtime.code,
        layers=lambda_runtime.layers,
        environment=common_env,
    )
    profile_fn = _lambda.Function(
        scope,
        f"{prefix}SpreadsheetProfileFunction",
        function_name=lambda_name_for_process(
            company, environment, "all", Process.SPREADSHEET_PROFILE
        ),
        runtime=_lambda.Runtime.PYTHON_3_12,
        handler="hiveflow.spreadsheet.handlers.profile_handler",
        timeout=Duration.minutes(5),
        memory_size=1024,
        description="Spreadsheet Engine: profile spreadsheet table candidates",
        code=lambda_runtime.code,
        layers=lambda_runtime.layers,
        environment=common_env,
    )
    # interpret + propose run the vendored Claude Agent SDK (Node + `claude` CLI),
    # so they ship as container images instead of the wheel-only zip. The Step
    # Functions chain, `job_id` payload, and IAM below are unchanged.
    agent_env = {
        **common_env,
        "ANTHROPIC_MODEL": _AGENT_MODEL,
        "MAX_BUDGET_USD": _AGENT_MAX_BUDGET_USD,
        # HIVEFLOW_AGENT_RUNTIME=sdk and CLAUDE_CODE_USE_BEDROCK=1 are baked into
        # the image; HIVEFLOW_BEDROCK_MODEL_ID (in common_env) drives the
        # converse fallback if the CLI is somehow unavailable.
    }
    # No explicit function_name: switching zip -> container image forces a
    # CloudFormation replacement, and CFN refuses to replace a custom-named
    # resource (name collision mid-update). These two are referenced only by
    # ARN (the Step Functions tasks below); nothing looks them up by name.
    interpret_fn = _lambda.DockerImageFunction(
        scope,
        f"{prefix}SpreadsheetInterpretFunction",
        code=_agent_image_code("hiveflow.spreadsheet.handlers.interpret_handler"),
        timeout=Duration.minutes(15),
        memory_size=2048,
        description="Spreadsheet Engine: Agent SDK semantic analysis of spreadsheet tables",
        environment=agent_env,
    )
    propose_fn = _lambda.DockerImageFunction(
        scope,
        f"{prefix}SpreadsheetProposeFunction",
        code=_agent_image_code("hiveflow.spreadsheet.handlers.propose_handler"),
        timeout=Duration.minutes(15),
        memory_size=2048,
        description="Spreadsheet Engine: Agent SDK transformation proposals",
        environment=agent_env,
    )

    for fn in (parse_fn, profile_fn, interpret_fn, propose_fn):
        data_bucket.grant_read_write(fn)
        grant_bedrock(fn)

    parse_task = _apply_lambda_throttle_retry(
        tasks.LambdaInvoke(
            scope,
            f"{prefix}SpreadsheetParseTask",
            lambda_function=parse_fn,
            output_path="$.Payload",
        )
    )
    profile_task = _apply_lambda_throttle_retry(
        tasks.LambdaInvoke(
            scope,
            f"{prefix}SpreadsheetProfileTask",
            lambda_function=profile_fn,
            output_path="$.Payload",
            payload=sfn.TaskInput.from_object(
                {
                    "job_id": sfn.JsonPath.string_at("$.job_id"),
                }
            ),
        )
    )
    interpret_task = _apply_lambda_throttle_retry(
        tasks.LambdaInvoke(
            scope,
            f"{prefix}SpreadsheetInterpretTask",
            lambda_function=interpret_fn,
            output_path="$.Payload",
            payload=sfn.TaskInput.from_object(
                {
                    "job_id": sfn.JsonPath.string_at("$.job_id"),
                }
            ),
        )
    )
    propose_task = _apply_lambda_throttle_retry(
        tasks.LambdaInvoke(
            scope,
            f"{prefix}SpreadsheetProposeTask",
            lambda_function=propose_fn,
            output_path="$.Payload",
            payload=sfn.TaskInput.from_object(
                {
                    "job_id": sfn.JsonPath.string_at("$.job_id"),
                }
            ),
        )
    )

    definition = parse_task.next(profile_task).next(interpret_task).next(propose_task)
    state_machine = sfn.StateMachine(
        scope,
        f"{prefix}SpreadsheetAnalyzeStateMachine",
        state_machine_name=step_function_name_for_process(
            company, environment, "all", Process.SPREADSHEET_ANALYZE
        ),
        definition_body=sfn.DefinitionBody.from_chainable(definition),
        # parse(5) + profile(5) + interpret(15) + propose(15) at max, plus retry
        # slack, exceeds 30m now that interpret/propose are agent-backed.
        timeout=Duration.minutes(50),
    )

    return {
        "state_machine": state_machine,
        "parse_function": parse_fn,
        "profile_function": profile_fn,
        "interpret_function": interpret_fn,
        "propose_function": propose_fn,
    }
