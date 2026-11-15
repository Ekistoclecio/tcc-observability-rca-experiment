# Canonical Log Revalidation — F4/F5

Existing F4 and F5 executions were reused. No fault was re-executed. Reprocessed datasets and requests were generated from committed implementation `3921f77f5f7979a30148d62c0799b826146bd82f`; source RAW hashes were verified unchanged.

## F5

Run: `iv-edv-pv-f5-01-001-f5-f5`. Ground Truth, Dataset A/B, RCA Request A/B, provenance, and raw immutability: **PASS**.
Canonical event classes A-only: **0**; B-only: **0**.

| Scenario | Metrics | Logs | Traces | Spans | Categories |
|---|---:|---:|---:|---:|---|
| A | 117 | 1361 | 85 | 2045 | `{"APPLICATION_DIAGNOSTIC": 1354, "APPLICATION_ERROR": 7, "HEALTH_CHECK": 6, "LIFECYCLE_NOISE": 72, "REDUNDANT_SUCCESS_EVENT": 15, "RUNTIME_NOISE": 18, "UNKNOWN": 55}` |
| B | 117 | 1399 | 85 | 2045 | `{"APPLICATION_DIAGNOSTIC": 1392, "APPLICATION_ERROR": 7, "UNKNOWN": 55}` |

F5 revalidation: **PASS**.

## F4

Run: `iv-edv-view-smoke1-001-f4-f4`; Ground Truth, provenance, and raw immutability: **PASS**. Stack-frame lines in the generated requests: **0**.
Application error logs: A=12, B=6; error span counts A=30, B=30.
A-only canonical class: `[["paymentservice", "ERROR", "APPLICATION_ERROR", "", "controlled application fault in paymentservice.charge"]]`
B-only canonical classes: `[]`
Canonical error message parity: **FAIL**.

The paymentservice `APPLICATION_ERROR` is present in A and absent from B logs. It was kept because error logs must not be removed to force parity. This is a parity failure under the new policy.

## Decision

**F4/F5 smoke: FAIL.** The final F1–F5 battery was not started. F5 passes; F4 does not pass under the same classifier, so proceeding would contradict the requirement to preserve A-only diagnostic errors.

Detailed machine-readable evidence: `audit/observability-contract/f4-log-revalidation.json` and `f5-log-revalidation.json`.
