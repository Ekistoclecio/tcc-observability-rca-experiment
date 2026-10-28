# Host experiment orchestrator

The orchestrator is part of the experimental control plane and runs on the host/WSL. It starts the existing Phase 3 open-loop runner, applies one explicitly selected fault, confirms the mechanism state, recovers it, and writes artifacts under `artifacts/experiments/<run_id>/`. It does not add experiment IDs to SUT requests and does not send its events to Loki A/B.

## Setup

### Recreate the local Kind cluster

The experiment cluster is pinned in `deployments/experiment/kind/cluster.yaml` to
`kindest/node:v1.35.8@sha256:07b2536e30b803ed61d1677a79df6115f798ce64c80f9e22f6ed45afd09323c0`.
Use Kind v0.33.0 and kubectl v1.35.x. The installer downloads/checksums kubectl to a
temporary tools directory; add it to `PATH` for setup and validation commands. The Kind
script refuses to overwrite an existing cluster; delete it explicitly before recreation.
It loads the ten local `tcc-phase2` application images.

```bash
export PATH="$(scripts/experiment/install-kubectl.sh):$PATH"
KIND_BIN="$(scripts/experiment/install-kind.sh)" scripts/experiment/create-kind-cluster.sh
scripts/experiment/setup-observability.sh
scripts/experiment/setup-chaos-mesh.sh
```

On a host without the local application images, build them sequentially first with
`TCC_LOAD_IMAGES=0 scripts/experiment/build-application-images.sh`; the cluster creation
script then loads those images after creating Kind. The experiment overlay installs the
Online Boutique, A/B backends, F4 hook configuration and keeps the original Kubernetes
`loadgenerator` at zero replicas.

### Install the experiment hook

The overlay has the F4 hook enabled but inert until its per-pod control file is created.
The setup helper can rebuild and verify the paymentservice image when required:

   ```bash
   scripts/experiment/setup-fault-injection.sh
   ```

For F1/F2, install the pinned Chaos Mesh release:

   ```bash
   scripts/experiment/setup-chaos-mesh.sh
   ```

   Chaos Mesh 2.8.6 documents Kubernetes 1.30–1.35 as its tested range. The pinned
   Kubernetes v1.35.8 combination is supported and needs no override. On another
   Kubernetes minor, setup stops unless `TCC_ALLOW_UNSUPPORTED_CHAOS_MESH=1` is set;
   that override is only for a documented compatibility smoke and is recorded in Ground
   Truth. The orchestrator preflight checks both the Kubernetes minor and installed Chaos
   Mesh controller/daemon version.

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
