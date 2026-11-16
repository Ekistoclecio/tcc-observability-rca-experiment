/*
 * Copyright 2026 Google LLC.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

package hipstershop;

import com.sun.net.httpserver.HttpServer;
import io.grpc.ForwardingServerCall;
import io.grpc.ForwardingServerCallListener;
import io.grpc.Metadata;
import io.grpc.ServerCall;
import io.grpc.ServerCallHandler;
import io.grpc.ServerInterceptor;
import io.grpc.Status;
import java.io.IOException;
import java.net.InetSocketAddress;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.time.Duration;
import java.util.Comparator;
import java.util.HexFormat;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.AtomicReference;
import java.util.concurrent.atomic.DoubleAdder;

/** Independent conventional tracing and metrics for the ad service. */
final class ConventionalTelemetry implements ServerInterceptor {
  static final String TRACE_HEADER_NAME = "x-tcc-trace-id";
  static final String SPAN_HEADER_NAME = "x-tcc-span-id";

  private static final Metadata.Key<String> TRACE_HEADER =
      Metadata.Key.of(TRACE_HEADER_NAME, Metadata.ASCII_STRING_MARSHALLER);
  private static final Metadata.Key<String> SPAN_HEADER =
      Metadata.Key.of(SPAN_HEADER_NAME, Metadata.ASCII_STRING_MARSHALLER);
  private static final SecureRandom RANDOM = new SecureRandom();

  private final boolean enabled;
  private final String serviceName;
  private final URI zipkinEndpoint;
  private final HttpClient httpClient;
  private final ConcurrentHashMap<MetricKey, Metric> metrics = new ConcurrentHashMap<>();

  ConventionalTelemetry(String serviceName) {
    this.serviceName = serviceName;
    this.enabled = "1".equals(System.getenv("ENABLE_CONVENTIONAL_TELEMETRY"));
    this.zipkinEndpoint =
        URI.create(
            System.getenv()
                .getOrDefault(
                    "CONVENTIONAL_ZIPKIN_ENDPOINT",
                    "http://jaeger-a.observability-a.svc.cluster.local:9411/api/v2/spans"));
    this.httpClient = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(2)).build();
    if (enabled) {
      startMetricsServer(
          Integer.parseInt(System.getenv().getOrDefault("CONVENTIONAL_METRICS_PORT", "9464")));
    }
  }

  @Override
  public <ReqT, RespT> ServerCall.Listener<ReqT> interceptCall(
      ServerCall<ReqT, RespT> call,
      Metadata headers,
      ServerCallHandler<ReqT, RespT> next) {
    String operation = call.getMethodDescriptor().getFullMethodName();
    if (operation.startsWith("grpc.health.")) {
      return next.startCall(call, headers);
    }

    String incomingTraceId = enabled ? headers.get(TRACE_HEADER) : null;
    final String traceId = enabled
        ? (incomingTraceId == null ? newId(16) : incomingTraceId)
        : "";
    final String parentId = enabled ? headers.get(SPAN_HEADER) : null;
    final String spanId = enabled ? newId(8) : "";
    long timestampMicros = System.currentTimeMillis() * 1000;
    long startedAtNanos = System.nanoTime();
    AtomicReference<Status> finalStatus = new AtomicReference<>(Status.OK);
    AtomicBoolean completed = new AtomicBoolean();

    ServerCall<ReqT, RespT> statusCall =
        new ForwardingServerCall.SimpleForwardingServerCall<>(call) {
          @Override
          public void close(Status status, Metadata trailers) {
            finalStatus.set(status);
            super.close(status, trailers);
          }
        };

    ServerCall.Listener<ReqT> listener;
    try {
      listener = next.startCall(statusCall, headers);
    } catch (RuntimeException error) {
      complete(
          traceId,
          spanId,
          parentId,
          operation,
          error.getClass().getSimpleName(),
          timestampMicros,
          startedAtNanos);
      throw error;
    }

    return new ForwardingServerCallListener.SimpleForwardingServerCallListener<>(listener) {
      private void finish() {
        if (!completed.compareAndSet(false, true)) {
          return;
        }
        Status status = finalStatus.get();
        String result = status.isOk() ? "OK" : status.getCode().name();
        complete(
            traceId,
            spanId,
            parentId,
            operation,
            result,
            timestampMicros,
            startedAtNanos);
      }

      @Override
      public void onComplete() {
        try {
          super.onComplete();
        } finally {
          finish();
        }
      }

      @Override
      public void onCancel() {
        finalStatus.set(Status.CANCELLED);
        try {
          super.onCancel();
        } finally {
          finish();
        }
      }
    };
  }

  private void complete(
      String traceId,
      String spanId,
      String parentId,
      String operation,
      String result,
      long timestampMicros,
      long startedAtNanos) {
    long durationMicros = Math.max(1, (System.nanoTime() - startedAtNanos) / 1000);
    double durationSeconds = durationMicros / 1_000_000.0;
    if (!enabled) {
      return;
    }
    Metric metric = metrics.computeIfAbsent(new MetricKey(operation, result), ignored -> new Metric());
    metric.count.incrementAndGet();
    metric.durationSeconds.add(durationSeconds);

    String parent = parentId == null ? "" : ",\"parentId\":\"" + json(parentId) + "\"";
    String body =
        "[{\"traceId\":\""
            + json(traceId)
            + "\",\"id\":\""
            + json(spanId)
            + "\""
            + parent
            + ",\"name\":\""
            + json(operation)
            + "\",\"kind\":\"SERVER\",\"timestamp\":"
            + timestampMicros
            + ",\"duration\":"
            + durationMicros
            + ",\"localEndpoint\":{\"serviceName\":\""
            + json(serviceName)
            + "\"},\"tags\":{\"result\":\""
            + json(result)
            + "\"}}]";
    HttpRequest request =
        HttpRequest.newBuilder(zipkinEndpoint)
            .timeout(Duration.ofSeconds(2))
            .header("content-type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(body))
            .build();
    httpClient
        .sendAsync(request, HttpResponse.BodyHandlers.discarding())
        .exceptionally(ignored -> null);
  }

  String renderMetrics() {
    StringBuilder output =
        new StringBuilder(
            "# HELP tcc_conventional_requests_total Completed conventional requests and RPCs.\n"
                + "# TYPE tcc_conventional_requests_total counter\n");
    metrics.entrySet().stream()
        .sorted(Comparator.comparing(entry -> entry.getKey().operation()))
        .forEach(
            entry -> {
              MetricKey key = entry.getKey();
              Metric metric = entry.getValue();
              String labels =
                  "service_name=\""
                      + label(serviceName)
                      + "\",operation=\""
                      + label(key.operation())
                      + "\",span_kind=\"server\",status_code=\""
                      + label(key.result())
                      + "\"";
              output
                  .append("tcc_conventional_requests_total{")
                  .append(labels)
                  .append("} ")
                  .append(metric.count.get())
                  .append('\n');
              output
                  .append("tcc_conventional_request_duration_seconds_sum{")
                  .append(labels)
                  .append("} ")
                  .append(metric.durationSeconds.sum())
                  .append('\n');
              output
                  .append("tcc_conventional_request_duration_seconds_count{")
                  .append(labels)
                  .append("} ")
                  .append(metric.count.get())
                  .append('\n');
            });
    return output.toString();
  }

  private void startMetricsServer(int port) {
    try {
      HttpServer server = HttpServer.create(new InetSocketAddress("0.0.0.0", port), 0);
      server.createContext(
          "/metrics",
          exchange -> {
            byte[] body = renderMetrics().getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().set("content-type", "text/plain; version=0.0.4");
            exchange.sendResponseHeaders(200, body.length);
            try (var response = exchange.getResponseBody()) {
              response.write(body);
            }
          });
      server.setExecutor(
          Executors.newSingleThreadExecutor(
              runnable -> {
                Thread thread = new Thread(runnable, "conventional-metrics");
                thread.setDaemon(true);
                return thread;
              }));
      server.start();
    } catch (IOException error) {
      throw new IllegalStateException("cannot start conventional metrics endpoint", error);
    }
  }

  private static String newId(int bytes) {
    byte[] value = new byte[bytes];
    RANDOM.nextBytes(value);
    return HexFormat.of().formatHex(value);
  }

  private static String json(String value) {
    return value.replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", "\\n");
  }

  private static String label(String value) {
    return json(value);
  }

  private record MetricKey(String operation, String result) {}

  private static final class Metric {
    private final AtomicLong count = new AtomicLong();
    private final DoubleAdder durationSeconds = new DoubleAdder();
  }
}
