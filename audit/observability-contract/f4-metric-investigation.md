# F4 metric parity investigation

## Result

The original F4 discrepancy was caused by two temporal effects, not by different `Charge` counter semantics or label fragmentation:

1. **Dataset Builder range-grid aliasing.** The Prometheus `query_range` used a 5 s evaluation step anchored at the arbitrary `window_start`. In the original run, the last evaluation was `2026-11-15T18:55:58.615Z`. It returned B=34 even though Prometheus had stored a newer B sample (40) at `18:55:58.732Z`, still before `window_end=18:55:59.777Z`. The 5 s evaluation grid therefore omitted an in-window point and produced B delta 7 instead of 13.
2. **Real B export/scrape lag at the cutoff.** At the exact same `window_end`, Prometheus held A=43 and B=40. B's 5 s OTel periodic metric reader exports to the Collector, and Prometheus B then scrapes the Collector every 5 s. A is served directly from the conventional service `/metrics` endpoint and scraped every 5 s. Thus a residual difference can exist at a strict shared cutoff even when the application increments are semantically identical.

The Builder now evaluates both Prometheus backends at a shared `step=1s`. Re-querying the original historical F4 window without injecting another fault changes the observed success delta from A=16/B=7 to A=16/B=13. The remaining difference of 3 is supported by the exact end-boundary values and is attributed to B's asynchronous export/scrape path. No values were interpolated, synthesized, or derived from traces.

## Original F4 request counter series

The original window was `2026-11-15T18:55:33.615Z` to `2026-11-15T18:55:59.777Z`. Each scenario had exactly one success series and one error series for `paymentservice / Charge`.

| Scenario | Native metric | Type | Outcome | Complete labels |
|---|---|---|---|---|
| A | `tcc_conventional_requests_total` | counter | success (`OK`) | `instance=paymentservice.default.svc.cluster.local:9464`, `job=online-boutique-conventional`, `operation=/hipstershop.PaymentService/Charge`, `service_name=paymentservice`, `span_kind=server`, `status_code=OK` |
| A | `tcc_conventional_requests_total` | counter | error (`13`) | same labels, `status_code=13` |
| B | `online_boutique_otel_tcc_otel_rpc_server_requests_total` | counter, exported but not the current normalized source | success (`OK`) | `deployment_environment_name=tcc-local`, `exported_job=paymentservice`, `instance=otel-collector:9464`, `job=otel-collector`, `otel_scope_name=online-boutique/paymentservice`, `otel_scope_version=0.10.7`, `rpc_method=/hipstershop.PaymentService/Charge`, `rpc_response_status_code=OK`, `rpc_system_name=grpc`, `service_name=paymentservice`, `service_version=0.10.7`, `telemetry_scenario=otel` |
| B | `online_boutique_otel_tcc_otel_rpc_server_requests_total` | counter, exported but not the current normalized source | error (`13`) | same labels, `rpc_response_status_code=13` |

The normalized B `request_count` currently selects `online_boutique_otel_tcc_otel_rpc_server_duration_seconds_count`; the direct B `...rpc_server_requests_total` counter is also exported. Both B series have identical values at every captured timestamp and are incremented in the same OTel completion callback. This source-selection difference is not the cause of the anomaly. A normalizes from `tcc_conventional_requests_total`. Full arrays for both native B series and histogram-count cross-checks are in [`f4-metric-value-anomaly.json`](f4-metric-value-anomaly.json). The original Prometheus RAW remains unchanged.

### Samples returned by the original 5 s query

| Scenario/outcome | Timestamped counter values |
|---|---|
| A success | `18:55:33.615=27`, `18:55:38.615=28`, `18:55:43.615=33`, `18:55:48.615=33`, `18:55:53.615=38`, `18:55:58.615=43` |
| B success | `18:55:33.615=27`, `18:55:38.615=27`, `18:55:43.615=27`, `18:55:48.615=30`, `18:55:53.615=33`, `18:55:58.615=34` |
| A error | `18:55:48.615=5`, `18:55:53.615=5`, `18:55:58.615=5` |
| B error | `18:55:53.615=2`, `18:55:58.615=5` |

At the window start both success counters had baseline 27. The error series first appeared after the window start in both scenarios. Its baseline is therefore invalid and its normalized value remains `null` with `missing_baseline`; this behavior was not changed.

## Scrape and query alignment

| Stage | A | B |
|---|---:|---:|
| Prometheus scrape interval | 5 s | 5 s |
| Application metric endpoint | Conventional service `/metrics` | Collector Prometheus exporter |
| Application-side OTel metric reader | — | 5 s periodic export |
| Previous Dataset Builder range step | 5 s | 5 s |
| Current Dataset Builder range step | 1 s | 1 s |

For the original run, the exact end-boundary query at `18:55:59.777Z` returned A=43 and B=40. The corresponding source sample timestamps were `18:55:58.561Z` and `18:55:58.732Z`. The old 5 s query grid ended before the latter sample. A/B scrape intervals are equal; the additional OTel reader stage and phase alignment explain the remaining three-count cutoff difference.

## Instrumentation semantics

Both implementations increment exactly once from the final unary gRPC completion callback, using the same success/error status:

- A's `conventionalTelemetry.wrapUnary` calls `record(operation, result, duration)` in `src/paymentservice/conventional-telemetry.js` and exposes its in-memory count through the service metrics endpoint.
- B's `otelTelemetry.wrapUnary` calls `requests.add(1, attributes)` and records duration in `src/paymentservice/otel-telemetry.js`; its callback is then passed to A's outer wrapper in `src/paymentservice/server.js`.
- Both wrappers guard completion against duplicate callbacks. Neither derives request metrics from spans.

The instrumented event is the completed `Charge` RPC in both cases. There was no paymentservice pod restart during the original run and the counter series are monotonic. No counter reset was detected.

## Series and Builder analysis

There was no fragmentation: A and B each had one success and one error series for this service/operation/outcome. Labels differ in native naming and resource dimensions, but both normalize to the same semantic operation and outcome. No `status`/`outcome`/`error.type` dimension split the B count.

The Builder issue was the Prometheus evaluation step, not label aggregation: the 5 s query grid could miss a newer stored sample inside the window. `scripts/experiment/dataset/core.py` now requests `step=1s` identically for A and B. Re-running that query against the original retained Prometheus data yields baseline 27 for both, final A=43/B=40, and deltas A=16/B=13. The error series still yield `null / missing_baseline`.

## One diagnostic F4 after the fix

One additional F4-only run, `f4-metric-investigation-20261115`, validated runtime endpoint behavior; no other fault class was run. The same 1 s Builder produced success deltas A=12/B=9 and error deltas A=6/B=6 for that run's own window. A high-frequency poll of the direct A and B metric endpoints showed eventual cumulative convergence: both reached success=84/error=11 at `19:31:44.996Z`, after the workload ended at `19:31:41.267Z`. Near the fault window end, the direct endpoints were A=84/11 and B=82/11. This confirms delayed B publication rather than a permanent lost increment. Full poll evidence is [`f4-metric-live-endpoint-poll.tsv`](f4-metric-live-endpoint-poll.tsv), and the re-query response is [`f4-metric-investigation-prometheus-query.json`](f4-metric-investigation-prometheus-query.json).

The diagnostic run's normalized deltas are not expected to equal the independent trace counts: traces were not used to calculate, correct, or validate metric values.

## Classification and disposition

- `SCRAPE_ALIGNMENT`: **present** — B has a periodic SDK export stage before its 5 s Prometheus scrape; the cutoff can precede its next published cumulative point.
- `BUILDER_AGGREGATION_BUG`: **present, temporal query-grid form** — 5 s `query_range` evaluation phase omitted a newer in-window B scrape. Fixed with a common 1 s query step.
- `SERIES_FRAGMENTATION`: **absent**.
- `MISSING_BASELINE`: **present only for the error series**; existing `null / missing_baseline` handling retained.
- `COUNTER_RESET`: **absent**.
- `DIFFERENT_INCREMENT_SEMANTICS`: **absent**.
- `INSTRUMENTATION_BUG`: **not found**.

The metric contract is considered semantically valid with an explicit collection-lag limitation. The original 16/7 discrepancy is explained: 6 counts came from range-grid aliasing and 3 remain as actual asynchronous collection lag at the common window cutoff. No artificial equality was imposed and no instrumentation, logs, traces, fault definitions, or Ground Truth were changed.

## Validation

- Dataset unit tests: 24 passed.
- Experiment unit suite (all seven test modules): 73 passed.
- Original F4 Prometheus data re-queried at 1 s without another fault: A=16, B=13.
- One F4 diagnostic run: valid injection/recovery; eventual endpoint counters converged.
- No F1, F2, F3, or F5 was run for this investigation.
