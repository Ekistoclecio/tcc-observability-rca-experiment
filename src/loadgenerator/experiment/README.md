# TCC controlled workload

The official experimental executor is the runner on the host/WSL. It sends
ordinary HTTP requests to the Online Boutique frontend; it is part of the
experimental control plane, not the SUT. It does not add TCC headers, telemetry,
or control IDs to requests. The original Online Boutique `locustfile.py` remains
unchanged. The Kubernetes Job is retained as an auxiliary executor and is not
used for official experiment runs.

The runner schedules flow arrivals open-loop at `TCC_TARGET_FLOW_RATE` flows
per second. It does not wait for earlier flows to finish. Each arrival gets a
new `requests.Session` and cookie jar, then executes exactly five requests:
`GET /`, `GET /product/{id}`, `POST /cart`, `GET /cart`, and
`POST /cart/checkout`. No `/cart/empty` reset is sent. Each flow's ID remains in
host-side metadata only.

`TCC_MAX_IN_FLIGHT_FLOWS` caps active flows. An arrival scheduled while the cap
is reached is recorded as a control-plane drop and is not queued or counted as
a SUT failure. Scheduler-late slots are also dropped rather than accumulated
into a catch-up burst. Configured arrival rate, observed start rate, and flow
completion throughput are reported separately.

Values in the validation environment file are low-load development defaults,
not official experiment settings. See
`docs/experiment/phase-03-controlled-workload.md` for the method and actual
validation results.

## Host/WSL run

```bash
scripts/experiment/setup-workload-venv.sh
KUBECONFIG=/path/to/kubeconfig \
TCC_ENV_FILE=src/loadgenerator/experiment/workload-validation.env.example \
TCC_RUN_ID=validation-001 \
scripts/experiment/run-workload.sh
```

The wrapper checks SUT/backend readiness, verifies the original loadgenerator
has zero replicas, creates a temporary frontend port-forward to localhost, and
writes JSON control metadata under `artifacts/workload/runs/`. The port-forward
is used because the current Kind cluster does not map a frontend NodePort to
the host. Do not run the original loadgenerator concurrently.

## Auxiliary in-cluster Job

`deployments/experiment/workload/` retains an optional Kubernetes Job for
diagnostic/manual use. Its results are not official workload runs and should
not be mixed with host-runner results. The Job shares the same workload code
and safe validation defaults. Apply it only when explicitly testing the
auxiliary execution path.
