# Shared Observation Smoke — F4/F5

- Validation ID: `so-115b`
- Frozen commit: `4248b042d92b86503a77490190ead62c9eda1f6b`
- Scope: one fresh F4 and one fresh F5 run; no F1/F2/F3 and no LLM.
- Overall: **FAIL** under the requirement that all model-visible diagnostic events originate from shared semantic observation points.

## F4 — iv-edv-so-115b-001-f4-f4

- Ground Truth: `valid`; package/provenance validation: `True`.
- Metrics A/B (model-visible): 113 / 117; semantic keys equal: `False`.
- Logs A/B: 1754 / 1802; canonical event identities equal: `False`.
- Logical spans A/B: 2656 / 2656; canonical logical span keys equal: `True`.
- Shared fault-relevant application event counts: A=6, B=6.
- A Only canonical log classes: 0 classes / 0 records.
- B Only canonical log classes: 4 classes / 48 records.
  - 22 × `adservice | INFO | APPLICATION_DIAGNOSTIC |  | received ad request (context_words=[])`
  - 2 × `cartservice | INFO | APPLICATION_DIAGNOSTIC |  | getcartasync called with userid={userid}`
  - 22 × `adservice | INFO | APPLICATION_DIAGNOSTIC |  | received ad request (context_words=[accessories])`
  - 2 × `cartservice | INFO | APPLICATION_DIAGNOSTIC |  | emptycartasync called with userid={userid}`

## F5 — iv-edv-so-115b-002-f5-f5

- Ground Truth: `valid`; package/provenance validation: `True`.
- Metrics A/B (model-visible): 117 / 117; semantic keys equal: `True`.
- Logs A/B: 1527 / 1568; canonical event identities equal: `False`.
- Logical spans A/B: 2329 / 2329; canonical logical span keys equal: `True`.
- Shared fault-relevant application event counts: A=4, B=4.
- A Only canonical log classes: 1 classes / 4 records.
  - 4 × `cartservice | ERROR | APPLICATION_ERROR |  | datastore_operation_failed operation=get`
- B Only canonical log classes: 5 classes / 45 records.
  - 1 × `cartservice | INFO | APPLICATION_DIAGNOSTIC |  | getcartasync called with userid={userid}`
  - 21 × `adservice | INFO | APPLICATION_DIAGNOSTIC |  | received ad request (context_words=[])`
  - 17 × `adservice | INFO | APPLICATION_DIAGNOSTIC |  | received ad request (context_words=[accessories])`
  - 2 × `cartservice | INFO | APPLICATION_DIAGNOSTIC |  | emptycartasync called with userid={userid}`
  - 4 × `cartservice | ERROR | APPLICATION_ERROR |  | datastore_operation_failed operation={operation}`
- Interpretation: The shared datastore error call is present 4x in both scenarios, but the current canonical log identity compares the rendered A body with the structured B template; A body has operation=GET while B body has a placeholder plus Operation=GET. This is a representation normalization issue, not evidence that the source event differs.

## Stop decision

F4/F5 smoke is **FAIL**. The shared `payment_charge_failed` event is present in both scenarios with neutral message and native B correlation preserved; however, additional B-only diagnostic log classes remain (including Java `adservice` and legacy `cartservice` events). In F5 the same datastore error logger call reaches both adapters, but the canonical log identity does not yet normalize its rendered-vs-template representation. These mismatches must be handled before any F1–F5 final battery.

No code or experimental configuration was modified during either run. No F1/F2/F3 run, model call, evaluator, or Phase 7 work was started.
