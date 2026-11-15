// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//      https://www.apache.org/licenses/LICENSE-2.0

using System;
using System.Collections.Concurrent;
using System.Diagnostics;
using System.Globalization;
using System.Linq;
using System.Net.Http;
using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Grpc.Core;
using Grpc.Core.Interceptors;
using Microsoft.Extensions.Configuration;

namespace cartservice
{
    internal sealed record ConventionalTraceContext(string TraceId, string SpanId);

    internal sealed class ConventionalMetric
    {
        internal long Count;
        internal double DurationSeconds;
        internal readonly object Sync = new object();
    }

    public sealed class ConventionalTelemetry
    {
        internal const string TraceHeader = "x-tcc-trace-id";
        internal const string SpanHeader = "x-tcc-span-id";

        private static readonly AsyncLocal<ConventionalTraceContext> CurrentContext = new AsyncLocal<ConventionalTraceContext>();
        private readonly ConcurrentDictionary<string, ConventionalMetric> _metrics = new ConcurrentDictionary<string, ConventionalMetric>();
        private readonly HttpClient _httpClient = new HttpClient { Timeout = TimeSpan.FromSeconds(2) };
        private readonly string _zipkinEndpoint;

        public ConventionalTelemetry(IConfiguration configuration)
        {
            Enabled = configuration["ENABLE_CONVENTIONAL_TELEMETRY"] == "1";
            _zipkinEndpoint = configuration["CONVENTIONAL_ZIPKIN_ENDPOINT"]
                ?? "http://jaeger-conventional.observability-conventional.svc.cluster.local:9411/api/v2/spans";
            if (Enabled)
            {
                var metricsPort = int.TryParse(configuration["CONVENTIONAL_METRICS_PORT"], out var configuredPort)
                    ? configuredPort
                    : 9464;
                _ = Task.Run(() => ServeMetricsAsync(metricsPort));
            }
        }

        public bool Enabled { get; }

        internal ConventionalTraceContext Current => CurrentContext.Value;

        internal IDisposable PushServerContext(string traceId, string spanId)
        {
            var previous = CurrentContext.Value;
            CurrentContext.Value = new ConventionalTraceContext(traceId, spanId);
            return new RestoreContext(() => CurrentContext.Value = previous);
        }

        public ConventionalClientSpan StartClientSpan(string operation)
        {
            if (!Enabled || Current == null)
            {
                return ConventionalClientSpan.Disabled;
            }

            return new ConventionalClientSpan(this, Current.TraceId, NewId(8), Current.SpanId, operation);
        }

        internal void CompleteSpan(string traceId, string spanId, string parentId, string operation, string kind, string result, long startedAt, double? observedDurationSeconds = null)
        {
            if (!Enabled)
            {
                return;
            }

            var elapsed = observedDurationSeconds.HasValue
                ? TimeSpan.FromSeconds(observedDurationSeconds.Value)
                : Stopwatch.GetElapsedTime(startedAt);
            var key = string.Join("\u001f", operation, kind.ToLowerInvariant(), result);
            var metric = _metrics.GetOrAdd(key, _ => new ConventionalMetric());
            lock (metric.Sync)
            {
                metric.Count++;
                metric.DurationSeconds += elapsed.TotalSeconds;
            }

            var timestamp = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() * 1000 - (long)elapsed.TotalMicroseconds;
            var parent = string.IsNullOrEmpty(parentId) ? string.Empty : $",\"parentId\":\"{parentId}\"";
            var span = $"[{{\"traceId\":\"{traceId}\",\"id\":\"{spanId}\"{parent}," +
                $"\"name\":\"{EscapeJson(operation)}\",\"kind\":\"{kind}\",\"timestamp\":{timestamp}," +
                $"\"duration\":{Math.Max(1, (long)elapsed.TotalMicroseconds)},\"localEndpoint\":{{\"serviceName\":\"cartservice\"}}," +
                $"\"tags\":{{\"telemetry.scenario\":\"conventional\",\"service.version\":\"0.10.7\",\"result\":\"{EscapeJson(result)}\"}}}}]";
            _ = ExportAsync(span);
        }

        private async Task ExportAsync(string span)
        {
            try
            {
                using var content = new StringContent(span, Encoding.UTF8, "application/json");
                using var response = await _httpClient.PostAsync(_zipkinEndpoint, content).ConfigureAwait(false);
            }
            catch
            {
                // Telemetry export must never change application behavior.
            }
        }

        public string RenderMetrics()
        {
            var output = new StringBuilder();
            output.AppendLine("# HELP tcc_conventional_requests_total Completed conventional requests and RPCs.");
            output.AppendLine("# TYPE tcc_conventional_requests_total counter");
            foreach (var entry in _metrics.OrderBy(item => item.Key, StringComparer.Ordinal))
            {
                var labels = entry.Key.Split('\u001f');
                long count;
                double duration;
                lock (entry.Value.Sync)
                {
                    count = entry.Value.Count;
                    duration = entry.Value.DurationSeconds;
                }
                var labelSet = $"service_name=\"cartservice\",operation=\"{Escape(labels[0])}\",span_kind=\"{Escape(labels[1])}\",status_code=\"{Escape(labels[2])}\"";
                output.Append("tcc_conventional_requests_total{").Append(labelSet).Append("} ").Append(count).AppendLine();
                output.Append("tcc_conventional_request_duration_seconds_sum{").Append(labelSet).Append("} ")
                    .Append(duration.ToString("R", CultureInfo.InvariantCulture)).AppendLine();
                output.Append("tcc_conventional_request_duration_seconds_count{").Append(labelSet).Append("} ").Append(count).AppendLine();
            }
            return output.ToString();
        }

        private async Task ServeMetricsAsync(int port)
        {
            var listener = new TcpListener(IPAddress.Any, port);
            listener.Start();
            while (true)
            {
                using var client = await listener.AcceptTcpClientAsync().ConfigureAwait(false);
                var body = Encoding.UTF8.GetBytes(RenderMetrics());
                var header = Encoding.ASCII.GetBytes(
                    $"HTTP/1.1 200 OK\r\nContent-Type: text/plain; version=0.0.4\r\nContent-Length: {body.Length}\r\nConnection: close\r\n\r\n");
                using var stream = client.GetStream();
                await stream.WriteAsync(header).ConfigureAwait(false);
                await stream.WriteAsync(body).ConfigureAwait(false);
            }
        }

        internal static string NewId(int bytes) => Convert.ToHexString(RandomNumberGenerator.GetBytes(bytes)).ToLowerInvariant();

        private static string Escape(string value) => value.Replace("\\", "\\\\", StringComparison.Ordinal).Replace("\"", "\\\"", StringComparison.Ordinal);

        private static string EscapeJson(string value) => Escape(value)
            .Replace("\r", "\\r", StringComparison.Ordinal)
            .Replace("\n", "\\n", StringComparison.Ordinal)
            .Replace("\t", "\\t", StringComparison.Ordinal);

        private sealed class RestoreContext : IDisposable
        {
            private readonly Action _restore;
            internal RestoreContext(Action restore) => _restore = restore;
            public void Dispose() => _restore();
        }
    }

    public sealed class ConventionalClientSpan : IDisposable
    {
        internal static readonly ConventionalClientSpan Disabled = new ConventionalClientSpan();
        private readonly ConventionalTelemetry _telemetry;
        private readonly string _traceId;
        private readonly string _spanId;
        private readonly string _parentId;
        private readonly string _operation;
        private readonly long _startedAt;
        private string _result = "OK";
        private double? _observedDurationSeconds;

        private ConventionalClientSpan() { }

        internal ConventionalClientSpan(ConventionalTelemetry telemetry, string traceId, string spanId, string parentId, string operation)
        {
            _telemetry = telemetry;
            _traceId = traceId;
            _spanId = spanId;
            _parentId = parentId;
            _operation = operation;
            _startedAt = Stopwatch.GetTimestamp();
        }

        public void SetError(Exception exception) => _result = exception.GetType().Name;

        public void CompleteObservation(double durationSeconds, bool failed)
        {
            _observedDurationSeconds = durationSeconds;
            _result = failed ? "ERROR" : "OK";
        }

        public void Dispose() => _telemetry?.CompleteSpan(_traceId, _spanId, _parentId, _operation, "CLIENT", _result, _startedAt, _observedDurationSeconds);
    }

    public sealed class ConventionalTelemetryInterceptor : Interceptor
    {
        private readonly ConventionalTelemetry _telemetry;

        public ConventionalTelemetryInterceptor(ConventionalTelemetry telemetry) => _telemetry = telemetry;

        public override async Task<TResponse> UnaryServerHandler<TRequest, TResponse>(
            TRequest request,
            ServerCallContext context,
            UnaryServerMethod<TRequest, TResponse> continuation)
        {
            if (context.Method.Contains("grpc.health", StringComparison.Ordinal))
            {
                return await continuation(request, context).ConfigureAwait(false);
            }

            var traceId = _telemetry.Enabled
                ? context.RequestHeaders.GetValue(ConventionalTelemetry.TraceHeader) ?? ConventionalTelemetry.NewId(16)
                : string.Empty;
            var parentId = _telemetry.Enabled ? context.RequestHeaders.GetValue(ConventionalTelemetry.SpanHeader) : null;
            var spanId = _telemetry.Enabled ? ConventionalTelemetry.NewId(8) : string.Empty;
            var startedAt = Stopwatch.GetTimestamp();
            var result = "OK";
            using var scope = _telemetry.Enabled ? _telemetry.PushServerContext(traceId, spanId) : null;
            try
            {
                return await continuation(request, context).ConfigureAwait(false);
            }
            catch (RpcException exception)
            {
                result = exception.StatusCode.ToString();
                throw;
            }
            catch
            {
                result = "UNKNOWN";
                throw;
            }
            finally
            {
                var durationSeconds = Stopwatch.GetElapsedTime(startedAt).TotalSeconds;
                CartTelemetry.RecordRpcObservation(context.Method, durationSeconds, result);
                _telemetry.CompleteSpan(traceId, spanId, parentId, context.Method, "SERVER", result, startedAt, durationSeconds);
            }
        }
    }
}
