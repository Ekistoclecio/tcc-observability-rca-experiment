# Host experiment orchestrator

The orchestrator is part of the experimental control plane and runs on the host/WSL. It starts the existing Phase 3 open-loop runner, applies one explicitly selected fault, confirms the mechanism state, recovers it, and writes artifacts under `artifacts/experiments/<run_id>/`. It does not add experiment IDs to SUT requests and does not send its events to Loki A/B.

## Setup

1. Confirm the Online Boutique and both observability stacks are Ready; leave the original Kubernetes `loadgenerator` scaled to zero.
2. Apply the experiment overlay and build the guarded Charge hook when needed:

   ```bash
   scripts/experiment/setup-fault-injection.sh
   ```

3. For F1/F2, install the pinned Chaos Mesh release:

   ```bash
   scripts/experiment/setup-chaos-mesh.sh
   ```

   Chaos Mesh 2.8.6 documents Kubernetes 1.30–1.35 as its tested range. On another Kubernetes minor, setup stops unless `TCC_ALLOW_UNSUPPORTED_CHAOS_MESH=1` is set. An override is for a compatibility smoke and must be recorded in the resulting Ground Truth.

## Run one fault

```bash
scripts/experiment/run-experiment.sh \
  --config configs/experiment/faults/f4-payment-charge-exception.json \
  --run-id f4-r001
```

F1/F2 on an unsupported Kubernetes minor require the explicit `--allow-unsupported-chaos-mesh` option. The configuration fixes the fault class, target, parameters, timing and a low validation workload. These are pilot/smoke defaults, not official experiment intensities.

To prepare repetition IDs without changing the fault target or configuration:

```bash
scripts/experiment/run-experiment.sh \
  --config configs/experiment/faults/f3-payment-unavailable.json \
  --run-id f3 --repetitions 3
```

This creates `f3-r001` through `f3-r003` and refuses to overwrite existing run artifacts.

## Fault matrix

| Class | Target | Mechanism |
|---|---|---|
| F1 | checkoutservice | Chaos Mesh `StressChaos` CPU |
| F2 | checkoutservice → paymentservice | Chaos Mesh `NetworkChaos` delay, direction `to` |
| F3 | paymentservice | Deployment scale-to-zero; restore captured replica count |
| F4 | paymentservice `Charge` | Experimental, opt-in file flag; process remains Ready |
| F5 | redis-cart | Deployment scale-to-zero; restore captured replica count |

The F4 hook is inactive unless its deployment flag is enabled and the control file exists. Redis is not instrumented. F3/F5 do not add latency or degradation.

## Run artifacts

Each run contains `run-metadata.json` from the Phase 3 workload, `ground-truth.json` validated against `scripts/experiment/ground-truth/ground-truth.schema.json`, a combined control/workload `orchestrator-events.jsonl`, and a local runner log. Artifacts are ignored by Git. Ground Truth stores requested and mechanism-confirmed actions separately, with UTC and host monotonic timestamps. It does not derive fault truth from metrics, logs, or traces.

An execution is valid only when preflight, workload admission, injection and recovery are confirmed. Control-plane drops make the run ineligible; SUT request failures remain separate workload outcomes. Cleanup is attempted on exceptions, Ctrl+C and SIGTERM.
