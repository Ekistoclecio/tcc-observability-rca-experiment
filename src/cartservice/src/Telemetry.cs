// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//      https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Diagnostics.Metrics;
using System.Text.Json;
using Microsoft.Extensions.Logging;

namespace cartservice
{
    internal static class CartTelemetry
    {
        internal const string ActivitySourceName = "OnlineBoutique.CartService";
        internal const string MeterName = "OnlineBoutique.CartService";

        internal static readonly ActivitySource ActivitySource = new(ActivitySourceName, "0.10.7");
        internal static readonly Meter Meter = new(MeterName, "0.10.7");
        internal static readonly Counter<long> RedisOperations = Meter.CreateCounter<long>(
            "cart.redis.operations",
            unit: "{operation}",
            description: "Number of Redis operations issued by cartservice");
        internal static readonly Counter<long> RedisErrors = Meter.CreateCounter<long>(
            "cart.redis.errors",
            unit: "{error}",
            description: "Number of failed Redis operations issued by cartservice");
        internal static readonly Histogram<double> RedisDuration = Meter.CreateHistogram<double>(
            "cart.redis.operation.duration",
            unit: "s",
            description: "Duration of Redis operations issued by cartservice");
        internal static readonly Counter<long> RpcRequests = Meter.CreateCounter<long>(
            "tcc.otel.rpc.server.requests",
            unit: "{request}",
            description: "Completed application RPC requests");
        internal static readonly Counter<long> RpcErrors = Meter.CreateCounter<long>(
            "tcc.otel.rpc.server.errors",
            unit: "{error}",
            description: "Failed application RPC requests");
        internal static readonly Histogram<double> RpcDuration = Meter.CreateHistogram<double>(
            "tcc.otel.rpc.server.duration",
            unit: "s",
            description: "Application RPC request duration");

        internal static void RecordRpcObservation(string operation, double durationSeconds, string status)
        {
            var tags = new TagList
            {
                { "rpc.system.name", "grpc" },
                { "rpc.method", operation },
                { "rpc.response.status_code", status },
                { "outcome", status == "OK" ? "SUCCESS" : "ERROR" }
            };
            RpcRequests.Add(1, tags);
            if (status != "OK") RpcErrors.Add(1, tags);
            RpcDuration.Record(durationSeconds, tags);
        }

        internal static void EmitApplicationEvent(ILogger logger, string eventName, string operation,
            string outcome, string message)
        {
            // Build one semantic event, then let each deployment adapter encode it
            // independently. The A representation deliberately carries no trace IDs.
            var observation = new Dictionary<string, object>
            {
                ["timestamp"] = DateTimeOffset.UtcNow,
                ["service"] = "cartservice",
                ["severity"] = outcome == "ERROR" ? "ERROR" : "WARN",
                ["observation_event"] = true,
                ["event_name"] = eventName,
                ["operation"] = operation,
                ["outcome"] = outcome,
                ["message"] = message
            };
            if (Environment.GetEnvironmentVariable("ENABLE_CONVENTIONAL_TELEMETRY") == "1")
            {
                Console.WriteLine(JsonSerializer.Serialize(observation));
                return;
            }

            logger.LogError("{message} {observation_event} {event_name} {service} {severity} {operation} {outcome}",
                observation["message"], observation["observation_event"], observation["event_name"],
                observation["service"], observation["severity"], observation["operation"], observation["outcome"]);
        }
    }
}
