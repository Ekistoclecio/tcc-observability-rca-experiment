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

using System.Diagnostics;
using System.Diagnostics.Metrics;

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
    }
}
