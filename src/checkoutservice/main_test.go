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

package main

import (
	"context"
	"testing"
	"time"

	pb "github.com/GoogleCloudPlatform/microservices-demo/src/checkoutservice/genproto"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

type contextKey struct{}

func TestConvertCurrencyPropagatesContext(t *testing.T) {
	const contextValue = "checkout-request"
	wantDeadline := time.Now().Add(time.Minute)
	requestContext, cancel := context.WithDeadline(
		context.WithValue(context.Background(), contextKey{}, contextValue),
		wantDeadline,
	)
	defer cancel()

	conn, err := grpc.NewClient(
		"passthrough:///context-propagation-test",
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithUnaryInterceptor(func(ctx context.Context, method string, req, reply any, cc *grpc.ClientConn, invoker grpc.UnaryInvoker, opts ...grpc.CallOption) error {
			if got := ctx.Value(contextKey{}); got != contextValue {
				t.Errorf("context value = %v, want %q", got, contextValue)
			}
			gotDeadline, ok := ctx.Deadline()
			if !ok || !gotDeadline.Equal(wantDeadline) {
				t.Errorf("deadline = %v (present=%v), want %v", gotDeadline, ok, wantDeadline)
			}
			if ctx.Done() != requestContext.Done() {
				t.Error("cancellation signal was not propagated")
			}

			result := reply.(*pb.Money)
			result.CurrencyCode = "BRL"
			return nil
		}),
	)
	if err != nil {
		t.Fatalf("grpc.NewClient() error = %v", err)
	}
	defer conn.Close()

	svc := &checkoutService{currencySvcConn: conn}
	result, err := svc.convertCurrency(requestContext, &pb.Money{CurrencyCode: "USD", Units: 1}, "BRL")
	if err != nil {
		t.Fatalf("convertCurrency() error = %v", err)
	}
	if got := result.GetCurrencyCode(); got != "BRL" {
		t.Errorf("converted currency = %q, want BRL", got)
	}
}
