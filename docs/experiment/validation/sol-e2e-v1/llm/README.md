# GPT-6.1 Sol end-to-end validation protocol

This isolated validation run reuses the frozen `rca-prompt-v1` and
`rca-schema-v1` artifacts without copying or editing them. Its only model
configuration change from the Luna pilot is `gpt-6.1-sol`; reasoning, output,
sampling omissions, service tier, and tool restrictions are unchanged.

The ten inputs are the A/B RCA Inputs produced by the fresh physical run under
`../physical/results/`. The blind execution plan is deterministically derived
from their content hashes. Paths, fault identifiers, treatments, Ground Truth,
and this run identity remain control-side and are not request fields.

Pricing was checked against the official OpenAI pricing and GPT-6.1 Sol model
documentation on 2026-11-17. The snapshot records Standard short-context rates
and the documented long-context multiplier. Per-case cost uses API-reported
usage and the frozen snapshot; reasoning tokens are recorded separately and
are not added to output tokens a second time.

This is a pilot/end-to-end validation with five physical fault instances, one
physical execution per fault, two telemetry treatments, and ten logical RCA
diagnoses. It is not a statistical/generalizable evaluation.
