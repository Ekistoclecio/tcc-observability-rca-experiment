# Canonical Log Revalidation — F4/F5

No fault was re-executed. Existing RAW snapshots were reprocessed into separate derived artifact directories; source RAW hashes remained unchanged.

## F5 existing execution

Run: `iv-edv-pv-f5-01-001-f5-f5`

Ground Truth, Dataset A/B, request A/B, projection provenance, and raw immutability: **PASS**.

Canonical diagnostic event classes: A-only **0**, B-only **0**. F5 log parity: **PASS**.

| Scenario | Visible metrics | Visible logs | Traces | Spans | Category counts |
|---|---:|---:|---:|---:|---|
| A | 117 | 1361 | 85 | 2045 | `{"APPLICATION_DIAGNOSTIC": 1354, "APPLICATION_ERROR": 7, "HEALTH_CHECK": 6, "LIFECYCLE_NOISE": 72, "REDUNDANT_SUCCESS_EVENT": 15, "RUNTIME_NOISE": 18, "UNKNOWN": 55}` |
| B | 117 | 1399 | 85 | 2045 | `{"APPLICATION_DIAGNOSTIC": 1392, "APPLICATION_ERROR": 7, "UNKNOWN": 55}` |

F5 detailed evidence: `artifacts/integration-validation/canonical-log-revalidation-001/revalidation.md`.

## F4 existing execution regression

Run: `iv-edv-view-smoke1-001-f4-f4`; Ground Truth, A/B provenance, and RAW immutability: **PASS**. Stack frames in requests: **0**. Canonical error span counts: A=30, B=30.

Application error log records: A=12, B=6. Canonical error-message parity: **FAIL**.

| Scenario | Category counts |
|---|---|
| A | `{"APPLICATION_DIAGNOSTIC": 1773, "APPLICATION_ERROR": 12, "HEALTH_CHECK": 5, "LIFECYCLE_NOISE": 6, "REDUNDANT_SUCCESS_EVENT": 16, "RUNTIME_NOISE": 66, "UNKNOWN": 92}` |
| B | `{"APPLICATION_DIAGNOSTIC": 1824, "APPLICATION_ERROR": 6, "UNKNOWN": 62}` |

A-only canonical event classes: `[["paymentservice", "ERROR", "APPLICATION_ERROR", "", "controlled application fault in paymentservice.charge"]]`

B-only canonical event classes: `[]`

The A-only paymentservice `APPLICATION_ERROR` remains model-visible, as required. It was not dropped or reclassified to manufacture parity. This is a genuine log-coverage parity failure under the new shared policy.

## Decision

**F4/F5 smoke: FAIL.** The final F1–F5 battery was not started because F4 parity is not satisfied under this policy. No new fault was run and no silent retry occurred.

Machine-readable summary: `audit/observability-contract/f4-f5-log-smoke-summary.json`.
