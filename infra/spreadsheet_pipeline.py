from __future__ import annotations

from typing import Any

from aws_cdk import Duration, Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from constructs import Construct

from lambda_bundle import HiveFlowLambdaRuntime, hiveflow_lambda_runtime
from hiveflow.project_config import agent_pipelines_role_name, spreadsheet_engine_state_machine_name


def _create_shared_execution_role(scope: Construct, *, environment: str) -> iam.Role:
    """One execution role shared by every Spreadsheet Engine Lambda.

    A single, predictably-named role (rather than one auto-generated role per
    function) is what lets a single trust-policy entry on each company's tenant
    role (``hiveflow-portal-tenant-{company}-{environment}``) cover every
    Lambda in this pipeline.
    """
    role = iam.Role(
        scope,
        "SpreadsheetLambdaRole",
        role_name=agent_pipelines_role_name(environment),
        assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        managed_policies=[
            iam.ManagedPolicy.from_aws_managed_policy_name(
                "service-role/AWSLambdaBasicExecutionRole"
            ),
        ],
    )
    account = Stack.of(scope).account
    env_slug = environment.strip().lower()
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["sts:AssumeRole"],
            resources=[f"arn:aws:iam::{account}:role/hiveflow-portal-tenant-*-{env_slug}"],
        )
    )
    return role


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
    environment: str,
    lambda_runtime: HiveFlowLambdaRuntime,
    common_env: dict[str, str],
    grant_bedrock: Any,
) -> dict[str, Any]:
    """Spreadsheet Engine: parse -> profile -> interpret -> propose.

    Global/shared across every company — a single deployment invoked with
    ``company`` in the Step Functions input rather than one copy per company.
    Data access is via the caller-supplied ``company``'s tenant role
    (``hiveflow.tenant_credentials``), assumed inside each handler; these
    Lambdas hold no standing per-company S3 grant.
    """
    prefix = construct_id
    shared_role = _create_shared_execution_role(scope, environment=environment)

    parse_fn = _lambda.Function(
        scope,
        f"{prefix}SpreadsheetParseFunction",
        function_name=f"platform-{environment.strip().lower()}-spreadsheet-parse",
        runtime=_lambda.Runtime.PYTHON_3_12,
        handler="hiveflow.spreadsheet.handlers.parse_handler",
        timeout=Duration.minutes(5),
        memory_size=1024,
        description="Spreadsheet Engine: parse uploaded Excel workbooks",
        code=lambda_runtime.code,
        layers=lambda_runtime.layers,
        environment=common_env,
        role=shared_role,
    )
    profile_fn = _lambda.Function(
        scope,
        f"{prefix}SpreadsheetProfileFunction",
        function_name=f"platform-{environment.strip().lower()}-spreadsheet-profile",
        runtime=_lambda.Runtime.PYTHON_3_12,
        handler="hiveflow.spreadsheet.handlers.profile_handler",
        timeout=Duration.minutes(5),
        memory_size=1024,
        description="Spreadsheet Engine: profile spreadsheet table candidates",
        code=lambda_runtime.code,
        layers=lambda_runtime.layers,
        environment=common_env,
        role=shared_role,
    )
    # interpret + propose both talk to Bedrock directly (a native `converse`
    # tool-use loop for interpret, a plain single-shot call for propose) — no
    # Node, no `claude` CLI, so both are plain zip Lambdas like every other
    # stage. They need hiveflow-spreadsheet-parser's own deps (pandas,
    # python-calamine, pydantic) on top of the "full" set, hence the separate
    # "parser" runtime bundle instead of the shared `lambda_runtime`. This used
    # to be a container image (Node + the `claude` CLI via the Agent SDK) —
    # that path was found to silently fail on every real invocation (Bedrock's
    # own model-invocation logs showed a session-startup probe, an unused
    # session-title call, then a multi-minute silent hang before falling back
    # to `converse` anyway), so the same tools were ported onto Bedrock's own
    # tool-use protocol instead.
    parser_runtime = hiveflow_lambda_runtime(scope, profile="parser")
    interpret_fn = _lambda.Function(
        scope,
        f"{prefix}SpreadsheetInterpretFunction",
        function_name=f"platform-{environment.strip().lower()}-spreadsheet-interpret",
        runtime=_lambda.Runtime.PYTHON_3_12,
        handler="hiveflow.spreadsheet.handlers.interpret_handler",
        timeout=Duration.minutes(15),
        memory_size=2048,
        description="Spreadsheet Engine: Bedrock tool-use semantic analysis of spreadsheet tables",
        code=parser_runtime.code,
        layers=parser_runtime.layers,
        environment=common_env,
        role=shared_role,
    )
    propose_table_fn = _lambda.Function(
        scope,
        f"{prefix}SpreadsheetProposeFunction",
        function_name=f"platform-{environment.strip().lower()}-spreadsheet-propose",
        runtime=_lambda.Runtime.PYTHON_3_12,
        handler="hiveflow.spreadsheet.handlers.propose_table_handler",
        timeout=Duration.minutes(15),
        memory_size=2048,
        description="Spreadsheet Engine: Bedrock transformation proposal for one table",
        code=parser_runtime.code,
        layers=parser_runtime.layers,
        environment=common_env,
        role=shared_role,
    )
    # Plain zip Lambdas either side of the Map fan-out below — no Bedrock work,
    # just S3 reads/writes, so the "full" runtime (no spreadsheet-parser deps) is enough.
    propose_prepare_fn = _lambda.Function(
        scope,
        f"{prefix}SpreadsheetProposePrepareFunction",
        runtime=_lambda.Runtime.PYTHON_3_12,
        handler="hiveflow.spreadsheet.handlers.propose_prepare_handler",
        timeout=Duration.minutes(2),
        memory_size=512,
        description="Spreadsheet Engine: flip job to proposing, list table_ids to fan out over",
        code=lambda_runtime.code,
        layers=lambda_runtime.layers,
        environment=common_env,
        role=shared_role,
    )
    propose_finalize_fn = _lambda.Function(
        scope,
        f"{prefix}SpreadsheetProposeFinalizeFunction",
        runtime=_lambda.Runtime.PYTHON_3_12,
        handler="hiveflow.spreadsheet.handlers.propose_finalize_handler",
        timeout=Duration.minutes(5),
        memory_size=512,
        description="Spreadsheet Engine: aggregate per-table proposals into the final report",
        code=lambda_runtime.code,
        layers=lambda_runtime.layers,
        environment=common_env,
        role=shared_role,
    )

    # No standing S3 grant here — these are global/shared across every company,
    # so data access happens per-invocation via that company's assumed tenant
    # role (hiveflow.tenant_credentials); sts:AssumeRole on that wildcard is
    # already granted once on shared_role above. Only Bedrock needs a per-call grant.
    grant_bedrock(shared_role)

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
                    "company": sfn.JsonPath.string_at("$.company"),
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
                    "company": sfn.JsonPath.string_at("$.company"),
                }
            ),
        )
    )
    propose_prepare_task = _apply_lambda_throttle_retry(
        tasks.LambdaInvoke(
            scope,
            f"{prefix}SpreadsheetProposePrepareTask",
            lambda_function=propose_prepare_fn,
            output_path="$.Payload",
            payload=sfn.TaskInput.from_object(
                {
                    "job_id": sfn.JsonPath.string_at("$.job_id"),
                    "company": sfn.JsonPath.string_at("$.company"),
                }
            ),
        )
    )
    propose_table_task = _apply_lambda_throttle_retry(
        tasks.LambdaInvoke(
            scope,
            f"{prefix}SpreadsheetProposeTableTask",
            lambda_function=propose_table_fn,
            output_path="$.Payload",
            payload=sfn.TaskInput.from_object(
                {
                    "job_id": sfn.JsonPath.string_at("$.job_id"),
                    "table_id": sfn.JsonPath.string_at("$.table_id"),
                    "company": sfn.JsonPath.string_at("$.company"),
                }
            ),
        )
    )
    # A table's proposal (one Bedrock oracle-clean call) still takes real time;
    # running every table sequentially in a single 900s Lambda invocation would
    # time out on any workbook with more than a handful of tables. Fan out per
    # table instead; each branch only ever
    # writes its own table's S3 key, so branches can run concurrently without
    # racing on the shared job.json (that's written once, before and after,
    # by propose_prepare_task / propose_finalize_task).
    propose_map = sfn.Map(
        scope,
        f"{prefix}SpreadsheetProposeMap",
        items_path=sfn.JsonPath.string_at("$.table_ids"),
        item_selector={
            "job_id": sfn.JsonPath.string_at("$.job_id"),
            "table_id": sfn.JsonPath.string_at("$$.Map.Item.Value"),
            "company": sfn.JsonPath.string_at("$.company"),
        },
        max_concurrency=4,
        result_path=sfn.JsonPath.DISCARD,
    )
    propose_map.item_processor(propose_table_task)
    propose_finalize_task = _apply_lambda_throttle_retry(
        tasks.LambdaInvoke(
            scope,
            f"{prefix}SpreadsheetProposeFinalizeTask",
            lambda_function=propose_finalize_fn,
            output_path="$.Payload",
            payload=sfn.TaskInput.from_object(
                {
                    "job_id": sfn.JsonPath.string_at("$.job_id"),
                    "company": sfn.JsonPath.string_at("$.company"),
                }
            ),
        )
    )

    definition = (
        parse_task.next(profile_task)
        .next(interpret_task)
        .next(propose_prepare_task)
        .next(propose_map)
        .next(propose_finalize_task)
    )
    state_machine = sfn.StateMachine(
        scope,
        f"{prefix}SpreadsheetAnalyzeStateMachine",
        state_machine_name=spreadsheet_engine_state_machine_name(environment),
        definition_body=sfn.DefinitionBody.from_chainable(definition),
        # parse(5) + profile(5) + interpret(15) + one table's propose(15) at
        # max, plus retry slack. Tables run in parallel (max_concurrency=4) so
        # total wall time no longer scales with table count the way a single
        # sequential propose Lambda did.
        timeout=Duration.minutes(50),
    )

    return {
        "state_machine": state_machine,
        "parse_function": parse_fn,
        "profile_function": profile_fn,
        "interpret_function": interpret_fn,
        "propose_function": propose_table_fn,
        "propose_prepare_function": propose_prepare_fn,
        "propose_finalize_function": propose_finalize_fn,
    }
