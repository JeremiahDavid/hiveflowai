# Container image for the Spreadsheet Engine interpret + propose Lambdas.
#
# These stages run the vendored Claude Agent SDK (`HIVEFLOW_AGENT_RUNTIME=sdk`),
# which shells out to the `claude` Node CLI — impossible in the wheel-only zip
# Lambdas the other stages use. Everything else about the pipeline (Step
# Functions chain, `job_id` payload, S3 + Bedrock IAM) is unchanged; only these
# two functions swap to `_lambda.DockerImageFunction` in spreadsheet_pipeline.py.
#
# Build context is the repo root; see infra/spreadsheet_pipeline.py.

FROM public.ecr.aws/lambda/python:3.12

# Cache-buster wired from infra/spreadsheet_pipeline.py::AGENT_IMAGE_REVISION.
ARG AGENT_IMAGE_REVISION=dev
RUN echo "agent image revision: ${AGENT_IMAGE_REVISION}"

# ── Node.js + the Claude Code CLI ────────────────────────────────────────────
# The AL2023-based Lambda image ships `dnf` as a thin `microdnf` (no
# --allowerasing). nodejs20 is in the AL2023 repos; fall back to plain nodejs.
RUN ( dnf install -y nodejs20 nodejs20-npm || dnf install -y nodejs npm ) \
    && dnf clean all \
    && node --version \
    && npm --version \
    && npm install -g @anthropic-ai/claude-code \
    && claude --version

# ── Python dependencies ─────────────────────────────────────────────────────
COPY requirements.txt requirements-lambda-agent.txt ./
RUN pip install --no-cache-dir -r requirements-lambda-agent.txt -t "${LAMBDA_TASK_ROOT}"

# ── hiveflow source (merged namespace package + the two vendored top-levels) ──
# Directory COPYs merge file-by-file, which is what the PEP 420 `hiveflow`
# namespace package needs (mirrors infra/lambda_bundle.py::assemble_hiveflow_tree).
COPY packages/hiveflow-platform/src/hiveflow     ${LAMBDA_TASK_ROOT}/hiveflow
COPY packages/hiveflow-connectors/src/hiveflow   ${LAMBDA_TASK_ROOT}/hiveflow
COPY packages/hiveflow-lake/src/hiveflow         ${LAMBDA_TASK_ROOT}/hiveflow
COPY packages/hiveflow-dna/src/hiveflow          ${LAMBDA_TASK_ROOT}/hiveflow
COPY packages/hiveflow-portal/src/hiveflow       ${LAMBDA_TASK_ROOT}/hiveflow
COPY packages/hiveflow/src/hiveflow              ${LAMBDA_TASK_ROOT}/hiveflow
COPY packages/hiveflow-core/src/hiveflow_core                       ${LAMBDA_TASK_ROOT}/hiveflow_core
COPY packages/hiveflow-spreadsheet-parser/src/hiveflow_spreadsheet_parser \
     ${LAMBDA_TASK_ROOT}/hiveflow_spreadsheet_parser

COPY config.yaml ${LAMBDA_TASK_ROOT}/config.yaml
COPY process_config.yaml ${LAMBDA_TASK_ROOT}/process_config.yaml

# ── Agent SDK runtime env ───────────────────────────────────────────────────
# Lambda's rootfs is read-only except /tmp; the `claude` CLI needs a writable
# HOME + config dir. Bedrock routing + sdk runtime selection are baked in;
# ANTHROPIC_MODEL / MAX_BUDGET_USD come from the function's `environment`.
ENV HOME=/tmp \
    CLAUDE_CONFIG_DIR=/tmp/.claude \
    CLAUDE_CODE_USE_BEDROCK=1 \
    HIVEFLOW_AGENT_RUNTIME=sdk \
    PYTHONPATH=${LAMBDA_TASK_ROOT}

# Overridden per function (interpret_handler / propose_handler) in CDK.
CMD [ "hiveflow.spreadsheet.handlers.interpret_handler" ]
