# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Define a default value so it's not empty if the builder fails to provide it
ARG BUILDPLATFORM=linux/amd64

FROM ghcr.io/open-telemetry/opentelemetry-operator/autoinstrumentation-java:2.30.0@sha256:2f72eab291fef045b0e3dc225d9de57d46617af5412b7214802e5dcea833ff59 AS otel-agent

FROM --platform=$BUILDPLATFORM eclipse-temurin:25.0.4_7-jdk-noble@sha256:534968c051301957beae735e7ba1db54d99ddecf08746d3b9d4f318cc132dbc3 AS builder

WORKDIR /app

COPY ["build.gradle", "gradlew", "./"]
COPY gradle gradle
RUN chmod +x gradlew
RUN ./gradlew downloadRepos

COPY . .
RUN chmod +x gradlew
RUN ./gradlew installDist

FROM eclipse-temurin:25.0.4_7-jre-alpine@sha256:3137541deb3cac6626b5d9a4a2187bc0d6a34312f858bd2c67dd01e732e6b682

# @TODO: https://github.com/GoogleCloudPlatform/microservices-demo/issues/2517
# Download Stackdriver Profiler Java agent
# RUN mkdir -p /opt/cprof && \
#     wget -q -O- https://storage.googleapis.com/cloud-profiler/java/latest/profiler_java_agent_alpine.tar.gz \
#     | tar xzv -C /opt/cprof && \
#     rm -rf profiler_java_agent.tar.gz

WORKDIR /app
COPY --from=builder /app .
COPY --from=otel-agent /javaagent.jar /otel/javaagent.jar

ENV JAVA_TOOL_OPTIONS="-javaagent:/otel/javaagent.jar"

EXPOSE 9555
ENTRYPOINT ["/app/build/install/hipstershop/bin/AdService"]
