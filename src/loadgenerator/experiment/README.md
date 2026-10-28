# TCC controlled workload

This Locust workload is separate from the Online Boutique demonstration
`locustfile.py`. One virtual user runs a fixed cart reset followed by the
business flow `home -> product -> add cart -> cart -> checkout`; each user has
an independent Locust cookie jar. The Locust user label and flow ID are kept in
the control process and are not sent to the application.

The model is closed-loop: each virtual user starts its next flow only after its
previous flow and configured think time finish. `TCC_SPAWN_RATE` means users
spawned per second. It is not a request rate or flow arrival rate. No open-loop
target flow rate is implemented.

Values in `workload.env` are low-load validation defaults only. They are not
official experiment parameters. See `docs/experiment/phase-03-controlled-workload.md`
for the phases, metadata fields and validation results.

## Local controlled run

Create the isolated Python environment and run against the local Kind cluster:

```bash
scripts/experiment/setup-workload-venv.sh
KUBECONFIG=/path/to/kubeconfig \
TCC_ENV_FILE=src/loadgenerator/experiment/workload-validation.env.example \
TCC_RUN_ID=validation-001 \
scripts/experiment/run-workload.sh
```

The runner checks application and observability readiness, verifies the original
loadgenerator has zero replicas, opens a temporary frontend port-forward, and
writes JSON control metadata under `artifacts/workload/runs/`. Do not run the
original loadgenerator and this runner concurrently.

## Optional in-cluster Job

The Job is isolated from the original loadgenerator manifest. From the repository
root, build/load the image and apply the experiment overlay:

```bash
docker build -f deployments/experiment/workload/Dockerfile -t tcc-workload:phase3 .
docker image save tcc-workload:phase3 | docker exec -i tcc-baseline-control-plane ctr -n k8s.io images import -
kubectl apply -k deployments/experiment/workload
kubectl logs -f job/tcc-experimental-workload
```

The generated JSON is in `/results` inside the completed Job pod; copy it before
deleting the Job. Edit `deployments/experiment/workload/workload.env` to change
the validation parameters, then delete and reapply the Job because Jobs are
immutable. The in-cluster Job is optional; the local script is the runner used
for the recorded Phase 3 validations because it also captures before/after pod
state on the host.
