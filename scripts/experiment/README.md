# Host experiment orchestrator

## Build canonical RCA inputs (Phase 6)

The model-agnostic Canonical Full RCA Request is built offline from the Phase 5
normalized datasets only. It does not read Ground Truth, run metadata, orchestrator
events, workload logs, or observability backends. It makes no model/API calls and does
not truncate or select evidence.

Install the small JSON Schema validation dependency and build both scenario requests:

```bash
python3 -m pip install -r scripts/experiment/rca/requirements.txt
scripts/experiment/build-rca-input.sh --run-id <run_id>
```

Outputs are written to `artifacts/rca-inputs/<run_id>/scenario-{a,b}/` as
`request.json`, `request-preview.md`, and a control-plane-only `manifest.json`.
The run ID and dataset's root `scenario` field are not copied to the request or
preview. The builder removes only explicit treatment marker attributes such as
`telemetry_scenario` and `telemetry.scenario` from the model-visible copy; source
datasets remain unchanged. Native SDK/scope/resource attributes, metric names, IDs,
and other telemetry are retained, so the instrumentation treatment may still be
inferred from the evidence. Removal counts and retained native provenance are recorded
in the control-plane manifest. Existing output snapshots are not overwritten; `--replace` is an explicit
development-only replacement option. A single scenario may be built for development
with `--scenario a` or `--scenario b`.
For all run directories containing both datasets, use `--all-datasets`; this also
refuses to replace existing output unless `--replace` is explicitly supplied.

The versioned prompt and JSON Schemas live under `scripts/experiment/rca/`. The output
contract requires exactly three ordered, distinct hypotheses; helper validation checks
evidence references against the complete request. The preview is deterministically
rendered from the request itself. This is not a provider-specific API payload and has
not been tested against any model context window.

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

## Integrated engineering validation (Phase 6.5)

Run the fixed, non-scientific F1–F5 integration battery from the host/WSL:

```bash
scripts/experiment/run-integration-validation.sh \
  --validation-id integration-smoke-001 --all
```

The runner performs a global preflight and then reuses the existing Phase 4
orchestrator, Phase 2 observability validator, Phase 5 Dataset Builder, and Phase 6
Canonical RCA Request Builder. It executes one fresh run per selected fault, in fixed
F1-to-F5 order for `--all`. `--fault f4` runs only one class; `--dry-run` prints IDs,
configs, commands, and expected artifact locations without starting experiments.
Pre/post buffers default to the Phase 5 validation defaults and may be set with
`TCC_DATASET_PRE_BUFFER_SECONDS` and `TCC_DATASET_POST_BUFFER_SECONDS` or CLI flags.

Machine and human audit artifacts are written to the ignored directory
`artifacts/integration-validation/<validation-id>/`. This includes `commands.jsonl`,
before/during/after Kubernetes snapshots, raw command outputs, per-run
`validation.json`/`evidence-summary.md`, `campaign-summary.json`,
`validation-report.md`, and `evidence-index.md`. Large experiment, dataset, and request
files are referenced by path, SHA-256, and byte count instead of being duplicated.
Existing run/dataset/request artifacts cause a fail-closed no-overwrite error.

The report is engineering evidence only. It is not an official campaign and its
numbers must not be used to infer that telemetry scenario A or B is superior. Human
review remains explicit and is never marked complete by automation. This workflow ends
at the canonical A/B requests and performs no model calls or RCA scoring.

## Latest experiment results

Run a new battery from the host/WSL. This clears only `audit/results/`, then runs each
selected fault with a fresh run ID. The standard battery runs F1–F5 once each:

```bash
scripts/experiment/run-experiment-results.sh \
  --validation-id my-results-001 --all
```

The global `datasetA/` and `datasetB/` RAW files are captured directly from Prometheus,
Loki and Jaeger after the final fault, all against the same `experiment_start` to
`experiment_end` interval. Loki and Jaeger queries use bounded temporal segments which
are merged and deduplicated; the global files are not assembled from per-fault
captures. Each numbered fault folder contains the two Phase 5 normalized RCA-window
datasets and the byte-preserved Ground Truth from that same run. The Phase 6 RCA Inputs
remain in their existing per-run artifact directories.

To run a single fault or repeat a class, pass `--fault` multiple times in execution
order. For example, `--fault f1 --fault f1 --fault f2` produces
`001_cpu_saturation_r01`, `002_cpu_saturation_r02`, and
`003_network_latency_r01`. `--dry-run` prints the sequence and artifact sources without
clearing the previous results or starting a run. Pre/post RCA buffers can be set with
`--pre-buffer` and `--post-buffer`.

Use a new, unused validation ID for each battery. The runner fails before cleanup if
Kubernetes is not Ready or generated run IDs already exist. The cleanup guard accepts
only the exact repository path `<repo>/audit/results` and refuses symlinks or paths
outside it. Runtime results are ignored by Git. This is
engineering validation only: no AI is called and the output is not an official
scientific campaign.
