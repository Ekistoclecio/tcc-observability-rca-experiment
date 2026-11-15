# F5 Log Revalidation

Existing run reused: `iv-edv-pv-f5-01-001-f5-f5`. Fault was not re-executed.

Ground Truth: **PASS**; source RAW unchanged: **PASS**.

| Scenario | Metrics | Logs | Traces | Spans | Canonical log parity | Provenance | Categories |
|---|---:|---:|---:|---:|---|---|---|
| A | 117 | 1361 | 85 | 2045 | PASS | PASS | `{"APPLICATION_DIAGNOSTIC": 1354, "APPLICATION_ERROR": 7, "HEALTH_CHECK": 6, "LIFECYCLE_NOISE": 72, "REDUNDANT_SUCCESS_EVENT": 15, "RUNTIME_NOISE": 18, "UNKNOWN": 55}` |
| B | 117 | 1399 | 85 | 2045 | PASS | PASS | `{"APPLICATION_DIAGNOSTIC": 1392, "APPLICATION_ERROR": 7, "UNKNOWN": 55}` |

A-only canonical classes: 0; B-only: 0.
Unexpected removals/additions/mutations: 0/0/0.

Derived artifacts (new committed implementation): `artifacts/integration-validation/canonical-log-revalidation-002/f5/`.
