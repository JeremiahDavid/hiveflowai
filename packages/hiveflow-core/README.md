# hiveflow-core

Shared runtime for HiveFlow agentic components.

- `bedrock.py` — build `ClaudeAgentOptions` wired to Amazon Bedrock from the environment.
- `agent.py` — `run_agent()` helper: drive a `ClaudeSDKClient` turn loop to completion and collect output.
- `manifest.py` — cross-component data contracts (source provenance, JSON encoders).

Not published; consumed in-repo as an editable `pip` install (see the repo-root `requirements.txt`).
