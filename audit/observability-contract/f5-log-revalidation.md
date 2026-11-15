# F5 Log Revalidation

Existing execution reused: `iv-edv-pv-f5-01-001-f5-f5`; no fault was re-executed.

Ground Truth: **PASS**; source RAW unchanged: **PASS**.

| Scenario | Visible metrics | Visible logs | Visible traces | Spans | Provenance |
|---|---:|---:|---:|---:|---|
| A | 117 | 1361 | 85 | 2045 | PASS |
| B | 117 | 1399 | 85 | 2045 | PASS |

## Log category counts

### Scenario A

```json
{
  "categories": {
    "APPLICATION_DIAGNOSTIC": 1354,
    "APPLICATION_ERROR": 7,
    "HEALTH_CHECK": 6,
    "LIFECYCLE_NOISE": 72,
    "REDUNDANT_SUCCESS_EVENT": 15,
    "RUNTIME_NOISE": 18,
    "UNKNOWN": 55
  },
  "excluded_samples": {
    "HEALTH_CHECK": [
      {
        "body": "GetCartAsync called with userId={UserId}\nChecking CartService Health",
        "reason": "health_or_probe_event",
        "service": "cartservice",
        "severity": "INFO"
      },
      {
        "body": "GetCartAsync called with userId={UserId}\nChecking CartService Health",
        "reason": "health_or_probe_event",
        "service": "cartservice",
        "severity": "INFO"
      },
      {
        "body": "GetCartAsync called with userId={UserId}\nChecking CartService Health",
        "reason": "health_or_probe_event",
        "service": "cartservice",
        "severity": "INFO"
      },
      {
        "body": "GetCartAsync called with userId={UserId}\nChecking CartService Health",
        "reason": "health_or_probe_event",
        "service": "cartservice",
        "severity": "INFO"
      },
      {
        "body": "GetCartAsync called with userId={UserId}\nChecking CartService Health",
        "reason": "health_or_probe_event",
        "service": "cartservice",
        "severity": "INFO"
      }
    ],
    "LIFECYCLE_NOISE": [
      {
        "body": "1:M 15 Nov 2026 21:43:02.215 * <bf> \t{ bf-error-rate       :      0.01 }",
        "reason": "process_or_datastore_lifecycle",
        "service": "redis-cart",
        "severity": "ERROR"
      },
      {
        "body": "1:signal-handler (1794778972) Received SIGTERM scheduling shutdown...",
        "reason": "process_or_datastore_lifecycle",
        "service": "redis-cart",
        "severity": null
      },
      {
        "body": "1:M 15 Nov 2026 21:42:52.847 * User requested shutdown...",
        "reason": "process_or_datastore_lifecycle",
        "service": "redis-cart",
        "severity": null
      },
      {
        "body": "1:M 15 Nov 2026 21:42:52.848 * Saving the final RDB snapshot before exiting.",
        "reason": "process_or_datastore_lifecycle",
        "service": "redis-cart",
        "severity": null
      },
      {
        "body": "1:M 15 Nov 2026 21:42:52.851 * BGSAVE done, 45 keys saved, 0 keys skipped, 3844 bytes written.",
        "reason": "process_or_datastore_lifecycle",
        "service": "redis-cart",
        "severity": null
      }
    ],
    "REDUNDANT_SUCCESS_EVENT": [
      {
        "body": "Transaction processed: visa ending 0454     Amount: USD28.980000000",
        "reason": "successful_transaction_already_represented_by_request_metrics_and_trace_outcome",
        "service": "paymentservice",
        "severity": "INFO"
      },
      {
        "body": "Transaction processed: visa ending 0454     Amount: USD28.980000000",
        "reason": "successful_transaction_already_represented_by_request_metrics_and_trace_outcome",
        "service": "paymentservice",
        "severity": "INFO"
      },
      {
        "body": "Transaction processed: visa ending 0454     Amount: USD28.980000000",
        "reason": "successful_transaction_already_represented_by_request_metrics_and_trace_outcome",
        "service": "paymentservice",
        "severity": "INFO"
      },
      {
        "body": "Transaction processed: visa ending 0454     Amount: USD28.980000000",
        "reason": "successful_transaction_already_represented_by_request_metrics_and_trace_outcome",
        "service": "paymentservice",
        "severity": "INFO"
      },
      {
        "body": "Transaction processed: visa ending 0454     Amount: USD28.980000000",
        "reason": "successful_transaction_already_represented_by_request_metrics_and_trace_outcome",
        "service": "paymentservice",
        "severity": "INFO"
      }
    ],
    "RUNTIME_NOISE": [
      {
        "body": "env platform is either empty or invalid",
        "reason": "runtime_or_platform_message",
        "service": "frontend",
        "severity": null
      },
      {
        "body": "env platform is either empty or invalid",
        "reason": "runtime_or_platform_message",
        "service": "frontend",
        "severity": null
      },
      {
        "body": "env platform is either empty or invalid",
        "reason": "runtime_or_platform_message",
        "service": "frontend",
        "severity": null
      },
      {
        "body": "env platform is either empty or invalid",
        "reason": "runtime_or_platform_message",
        "service": "frontend",
        "severity": null
      },
      {
        "body": "env platform is either empty or invalid",
        "reason": "runtime_or_platform_message",
        "service": "frontend",
        "severity": null
      }
    ],
    "UNKNOWN": [
      {
        "body": "ENV_PLATFORM is: local",
        "reason": "no_explicit_application_signal_or_recognized_diagnostic_pattern",
        "service": "frontend",
        "severity": "DEBUG"
      },
      {
        "body": "ENV_PLATFORM is: local",
        "reason": "no_explicit_application_signal_or_recognized_diagnostic_pattern",
        "service": "frontend",
        "severity": "DEBUG"
      },
      {
        "body": "ENV_PLATFORM is: local",
        "reason": "no_explicit_application_signal_or_recognized_diagnostic_pattern",
        "service": "frontend",
        "severity": "DEBUG"
      },
      {
        "body": "view user cart",
        "reason": "no_explicit_application_signal_or_recognized_diagnostic_pattern",
        "service": "frontend",
        "severity": "DEBUG"
      },
      {
        "body": "ENV_PLATFORM is: local",
        "reason": "no_explicit_application_signal_or_recognized_diagnostic_pattern",
        "service": "frontend",
        "severity": "DEBUG"
      }
    ]
  },
  "model_visible_records": 1361,
  "raw_candidate_records": 1527
}
```

### Scenario B

```json
{
  "categories": {
    "APPLICATION_DIAGNOSTIC": 1392,
    "APPLICATION_ERROR": 7,
    "UNKNOWN": 55
  },
  "excluded_samples": {
    "UNKNOWN": [
      {
        "body": "Error status code '{StatusCode}' with detail '{Detail}' raised.",
        "reason": "no_explicit_application_signal_or_recognized_diagnostic_pattern",
        "service": "cartservice",
        "severity": "INFO"
      },
      {
        "body": "Error status code '{StatusCode}' with detail '{Detail}' raised.",
        "reason": "no_explicit_application_signal_or_recognized_diagnostic_pattern",
        "service": "cartservice",
        "severity": "INFO"
      },
      {
        "body": "Error status code '{StatusCode}' with detail '{Detail}' raised.",
        "reason": "no_explicit_application_signal_or_recognized_diagnostic_pattern",
        "service": "cartservice",
        "severity": "INFO"
      },
      {
        "body": "Error status code '{StatusCode}' with detail '{Detail}' raised.",
        "reason": "no_explicit_application_signal_or_recognized_diagnostic_pattern",
        "service": "cartservice",
        "severity": "INFO"
      },
      {
        "body": "Error status code '{StatusCode}' with detail '{Detail}' raised.",
        "reason": "no_explicit_application_signal_or_recognized_diagnostic_pattern",
        "service": "cartservice",
        "severity": "INFO"
      }
    ]
  },
  "model_visible_records": 1399,
  "raw_candidate_records": 1454
}
```

## Canonical event differences

A-only: `0`

B-only: `0`

## Provenance checks

### A

- unexpected removals: 0
- unexpected additions: 0
- unexpected mutations: 0
- evidence IDs: PASS
- item counts: PASS

### B

- unexpected removals: 0
- unexpected additions: 0
- unexpected mutations: 0
- evidence IDs: PASS
- item counts: PASS

## Overall: **PASS**
