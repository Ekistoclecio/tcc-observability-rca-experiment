# Build with repository root as context:
# docker build -f deployments/experiment/workload/Dockerfile -t tcc-workload:phase3 .
ARG PYTHON_BASE=python:3.14.7-alpine@sha256:05b2b8b732ecd268fee8727a369f936f022d1321b59befd13c30ede22769dcdc

FROM ${PYTHON_BASE} AS builder

RUN apk add --no-cache g++ linux-headers
WORKDIR /build
COPY src/loadgenerator/experiment/requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM ${PYTHON_BASE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/tcc \
    TCC_FRONTEND_URL=http://frontend:80 \
    TCC_ARTIFACT_DIR=/results

RUN apk add --no-cache libstdc++ \
    && addgroup -S workload \
    && adduser -S -G workload -u 10001 workload

WORKDIR /opt/tcc
COPY --from=builder /install /usr/local
COPY src/loadgenerator/experiment /opt/tcc/experiment
RUN mkdir -p /results && chown workload:workload /results

USER 10001:10001
ENTRYPOINT ["python", "-m", "experiment.runner"]
