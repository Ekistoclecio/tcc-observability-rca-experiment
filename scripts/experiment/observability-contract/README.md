# Common Observability Contract v1

The fault-independent contract is implemented by `scripts/experiment/dataset/contract.py` and described by the three JSON documents alongside this file. RAW Prometheus, Loki, and Jaeger responses remain unchanged; the contract applies only to normalized per-run RCA datasets.

## Metric contract

The common metric concepts are `request_count`, `request_duration`, and `cpu_utilization`. Counts and duration histograms use window deltas, not the cumulative final counter value. A counter reset is detected between adjacent samples and its post-reset increment is accumulated. A series is `missing_baseline` if it has fewer than two samples or its first sample occurs after the requested window start; this avoids interpreting a newly appearing cumulative series as a zero baseline. Histogram buckets are excluded. Duration is represented by count delta, sum delta, and average in seconds. CPU is derived from the same cAdvisor `container_cpu_usage_seconds_total` source, scraped independently by Prometheus A and the OTel Collector B, then represented as first/min/max/average/last CPU cores in the window. CPU cores are usage, not a percentage of a container limit.

Request dimensions are service, fixed logical operation, client/server/datastore kind, target when relevant, and success/error outcome. SDK, exporter, process, runtime, host and arbitrary resource labels are not copied into the common metric view. Native metric family names remain in `native_names` for audit.

## Log contract

Both scenarios retain SUT application log records with timestamp, service, severity when available, body and non-resource application attributes. Records are not filtered by severity or error. B's native `trace_id` and `span_id` remain present; A receives no synthetic correlation. SDK/resource and process/runtime identity fields are omitted from the compact RCA view. Stacktrace-only fields are omitted; application messages and error facts remain.

## Trace contract

The common logical span envelope uses service, operation, kind (`server`, `client`, `datastore`), target, start, duration, status, parent relationship, native trace/span IDs and selected semantic attributes. The static Online Boutique operation catalog is shared by A/B. Health, scrape and observability exporter spans are excluded as non-application noise. Exception stacktrace/type/message payloads on spans are excluded because they can be an automatic B-only diagnostic advantage; application log messages and span status remain. Traces are deduplicated by trace ID and spans by span ID.

## Parity and limitations

Selection and normalization are independent of the fault class and Ground Truth location. The Builder does not equalize counts, sample, truncate, choose errors, or consult the other scenario's output. Differences in trace IDs, parent context, log correlation, native names and representation are intentional. The before/after audit reports compare the fixed logical concepts; a missing concept is a parity failure, not a reason to filter its counterpart.

Runtime smoke was performed once each for F1, F4 and F5. The fixed metric/log/span concept inventories matched, and the common Builder tests pass. However, the F4 `paymentservice` `Charge` success counter deltas differ materially (A=16, B=7) even though both trace datasets contain 17 successful and 5 failed `Charge` spans in the same window. This is an unresolved metric instrumentation/export discrepancy; semantic metric parity is not accepted until explained or fixed. Both error metric series lack a valid baseline and are now emitted as `null`/`missing_baseline`, not as zero. cAdvisor CPU is the same logical source but scrape/export timing can differ between isolated backends. F2/F3 were not run.
