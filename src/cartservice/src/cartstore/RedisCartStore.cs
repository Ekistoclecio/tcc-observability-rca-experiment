// Copyright 2018 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//      http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

using System;
using System.Linq;
using System.Threading.Tasks;
using Grpc.Core;
using Microsoft.Extensions.Caching.Distributed;
using Google.Protobuf;
using System.Diagnostics;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.Logging;

namespace cartservice.cartstore
{
    public class RedisCartStore : ICartStore
    {
        private static readonly ActivitySource OtelActivitySource = new ActivitySource("cartservice.redis");
        private readonly IDistributedCache _cache;
        private readonly ConventionalTelemetry _telemetry;
        private readonly ILogger<RedisCartStore> _logger;

        public RedisCartStore(IDistributedCache cache, ConventionalTelemetry telemetry, ILogger<RedisCartStore> logger)
        {
            _cache = cache;
            _telemetry = telemetry;
            _logger = logger;
        }

        public async Task AddItemAsync(string userId, string productId, int quantity)
        {
            _logger.LogInformation("AddItemAsync called with userId={UserId}, productId={ProductId}, quantity={Quantity}", userId, productId, quantity);
            var started = Stopwatch.GetTimestamp();
            using var span = _telemetry.StartClientSpan("redis-cart GET/SET");
            using var otelSpan = StartOtelDatastoreSpan("GET SET");
            try
            {
                Hipstershop.Cart cart;
                var value = await _cache.GetAsync(userId);
                if (value == null)
                {
                    cart = new Hipstershop.Cart();
                    cart.UserId = userId;
                    cart.Items.Add(new Hipstershop.CartItem { ProductId = productId, Quantity = quantity });
                }
                else
                {
                    cart = Hipstershop.Cart.Parser.ParseFrom(value);
                    var existingItem = cart.Items.SingleOrDefault(i => i.ProductId == productId);
                    if (existingItem == null)
                    {
                        cart.Items.Add(new Hipstershop.CartItem { ProductId = productId, Quantity = quantity });
                    }
                    else
                    {
                        existingItem.Quantity += quantity;
                    }
                }
                await _cache.SetAsync(userId, cart.ToByteArray());
                span.CompleteObservation(Stopwatch.GetElapsedTime(started).TotalSeconds, false);
            }
            catch (Exception ex)
            {
                MarkOtelDatastoreFailure(otelSpan, ex);
                span.CompleteObservation(Stopwatch.GetElapsedTime(started).TotalSeconds, true);
                throw new RpcException(new Status(StatusCode.FailedPrecondition, $"Can't access cart storage. {ex}"));
            }
        }

        public async Task EmptyCartAsync(string userId)
        {
            _logger.LogInformation("EmptyCartAsync called with userId={UserId}", userId);
            var started = Stopwatch.GetTimestamp();
            using var span = _telemetry.StartClientSpan("redis-cart SET");
            using var otelSpan = StartOtelDatastoreSpan("SET");
            try
            {
                var cart = new Hipstershop.Cart();
                await _cache.SetAsync(userId, cart.ToByteArray());
                span.CompleteObservation(Stopwatch.GetElapsedTime(started).TotalSeconds, false);
            }
            catch (Exception ex)
            {
                MarkOtelDatastoreFailure(otelSpan, ex);
                span.CompleteObservation(Stopwatch.GetElapsedTime(started).TotalSeconds, true);
                throw new RpcException(new Status(StatusCode.FailedPrecondition, $"Can't access cart storage. {ex}"));
            }
        }

        public async Task<Hipstershop.Cart> GetCartAsync(string userId)
        {
            _logger.LogInformation("GetCartAsync called with userId={UserId}", userId);
            var started = Stopwatch.GetTimestamp();
            using var span = _telemetry.StartClientSpan("redis-cart GET");
            using var otelSpan = StartOtelDatastoreSpan("GET");
            try
            {
                // Access the cart from the cache
                var value = await _cache.GetAsync(userId);

                if (value != null)
                {
                    span.CompleteObservation(Stopwatch.GetElapsedTime(started).TotalSeconds, false);
                    return Hipstershop.Cart.Parser.ParseFrom(value);
                }

                // We decided to return empty cart in cases when user wasn't in the cache before
                span.CompleteObservation(Stopwatch.GetElapsedTime(started).TotalSeconds, false);
                return new Hipstershop.Cart();
            }
            catch (Exception ex)
            {
                MarkOtelDatastoreFailure(otelSpan, ex);
                span.CompleteObservation(Stopwatch.GetElapsedTime(started).TotalSeconds, true);
                throw new RpcException(new Status(StatusCode.FailedPrecondition, $"Can't access cart storage. {ex}"));
            }
        }

        public bool Ping()
        {
            try
            {
                return true;
            }
            catch (Exception)
            {
                return false;
            }
        }

        private static Activity StartOtelDatastoreSpan(string operation)
        {
            var activity = OtelActivitySource.StartActivity(operation, ActivityKind.Client);
            activity?.SetTag("db.system.name", "redis");
            activity?.SetTag("db.operation.name", operation);
            activity?.SetTag("server.address", "redis-cart");
            return activity;
        }

        private static void MarkOtelDatastoreFailure(Activity activity, Exception exception)
        {
            activity?.SetStatus(ActivityStatusCode.Error, exception.GetType().Name);
            activity?.SetTag("error.type", exception.GetType().FullName);
        }
    }
}
