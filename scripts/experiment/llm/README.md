# LLM/RCA runtime

This directory contains the offline request builder, OpenAI Responses API runner, response validator, retry policy, usage/cost accounting, and isolated tests. Canonical prompt, schema, model protocol, pricing schedule, and RCA Input schema live only in `resources/`.

The frozen default protocol uses `gpt-6-luna`. The runner locates the repository from its own source path and loads `<repository-root>/.env` with `python-dotenv` and `override=False`; `.env` must remain ignored and untracked. It never logs or persists the API key. Tests use synthetic fixtures and mocked clients, and do not call the network.

A fresh physical run generates `docs/experiment/results/`. After ten A/B RCA Inputs have been built and validated, run `python -m scripts.experiment.llm.runner freeze-plan`; that freezes the current inputs and deterministic blind order under `artifacts/experiment/llm/`. `control-smoke` and `run` are explicit network operations. The evaluator is a separate offline command in `scripts/experiment/evaluation/evaluator.py` and reads Ground Truth only after inference has been frozen.

Generated run/evaluation artifacts are outputs, not runtime source files. They are intentionally absent from the clean repository state.
