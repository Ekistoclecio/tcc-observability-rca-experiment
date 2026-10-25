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
using System.Diagnostics;
using System.Linq;
using System.Threading.Tasks;
using Grpc.Core;
using Microsoft.Extensions.Caching.Distributed;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.Logging;
using Google.Protobuf;

namespace cartservice.cartstore
{
    public class RedisCartStore : ICartStore
    {
        private readonly IDistributedCache _cache;
        private readonly ILogger<RedisCartStore> _logger;
        private readonly string _redisAddress;

        public RedisCartStore(IDistributedCache cache, IConfiguration configuration, ILogger<RedisCartStore> logger)
        {
            _cache = cache;
            _logger = logger;
            _redisAddress = configuration["REDIS_ADDR"] ?? "redis-cart:6379";
        }

        public async Task AddItemAsync(string userId, string productId, int quantity)
        {
            _logger.LogInformation("AddItemAsync called with userId={UserId}, productId={ProductId}, quantity={Quantity}", userId, productId, quantity);

            using var activity = StartRedisActivity("GET/SET");
            var startedAt = Stopwatch.GetTimestamp();
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
                RecordRedisOperation("GET/SET", startedAt, false);
            }
            catch (Exception ex)
            {
                RecordRedisOperation("GET/SET", startedAt, true);
                activity?.SetStatus(ActivityStatusCode.Error, ex.Message);
                throw new RpcException(new Status(StatusCode.FailedPrecondition, $"Can't access cart storage. {ex}"));
            }
        }

        public async Task EmptyCartAsync(string userId)
        {
            _logger.LogInformation("EmptyCartAsync called with userId={UserId}", userId);

            using var activity = StartRedisActivity("SET");
            var startedAt = Stopwatch.GetTimestamp();
            try
            {
                var cart = new Hipstershop.Cart();
                await _cache.SetAsync(userId, cart.ToByteArray());
                RecordRedisOperation("SET", startedAt, false);
            }
            catch (Exception ex)
            {
                RecordRedisOperation("SET", startedAt, true);
                activity?.SetStatus(ActivityStatusCode.Error, ex.Message);
                throw new RpcException(new Status(StatusCode.FailedPrecondition, $"Can't access cart storage. {ex}"));
            }
        }

        public async Task<Hipstershop.Cart> GetCartAsync(string userId)
        {
            _logger.LogInformation("GetCartAsync called with userId={UserId}", userId);

            using var activity = StartRedisActivity("GET");
            var startedAt = Stopwatch.GetTimestamp();
            try
            {
                // Access the cart from the cache
                var value = await _cache.GetAsync(userId);

                if (value != null)
                {
                    RecordRedisOperation("GET", startedAt, false);
                    return Hipstershop.Cart.Parser.ParseFrom(value);
                }

                // We decided to return empty cart in cases when user wasn't in the cache before
                RecordRedisOperation("GET", startedAt, false);
                return new Hipstershop.Cart();
            }
            catch (Exception ex)
            {
                RecordRedisOperation("GET", startedAt, true);
                activity?.SetStatus(ActivityStatusCode.Error, ex.Message);
                throw new RpcException(new Status(StatusCode.FailedPrecondition, $"Can't access cart storage. {ex}"));
            }
        }

        private Activity StartRedisActivity(string operation)
        {
            var activity = CartTelemetry.ActivitySource.StartActivity($"redis {operation}", ActivityKind.Client);
            activity?.SetTag("db.system.name", "redis");
            activity?.SetTag("db.operation.name", operation);

            var addressParts = _redisAddress.Split(':', 2);
            activity?.SetTag("server.address", addressParts[0]);
            if (addressParts.Length == 2 && int.TryParse(addressParts[1], out int port))
            {
                activity?.SetTag("server.port", port);
            }
            return activity;
        }

        private static void RecordRedisOperation(string operation, long startedAt, bool failed)
        {
            var tags = new TagList { { "db.operation.name", operation }, { "db.system.name", "redis" } };
            CartTelemetry.RedisOperations.Add(1, tags);
            if (failed)
            {
                CartTelemetry.RedisErrors.Add(1, tags);
            }
            CartTelemetry.RedisDuration.Record(Stopwatch.GetElapsedTime(startedAt).TotalSeconds, tags);
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
    }
}
