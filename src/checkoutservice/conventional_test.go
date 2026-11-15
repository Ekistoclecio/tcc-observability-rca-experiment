package main

import (
	"context"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
)

func testConventionalTelemetry() *conventionalTelemetry {
	return &conventionalTelemetry{
		enabled: true,
		service: "checkoutservice",
		metrics: make(map[conventionalMetricKey]conventionalMetricValue),
	}
}

func TestConventionalInterceptorsPropagateIndependentContext(t *testing.T) {
	telemetry := testConventionalTelemetry()
	incomingTraceID := "0123456789abcdef0123456789abcdef"
	incomingSpanID := "0123456789abcdef"
	serverContext := metadata.NewIncomingContext(context.Background(), metadata.Pairs(
		conventionalTraceHeader, incomingTraceID,
		conventionalSpanHeader, incomingSpanID,
		"traceparent", "00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01",
	))

	_, err := telemetry.UnaryServerInterceptor()(
		serverContext,
		nil,
		&grpc.UnaryServerInfo{FullMethod: "/hipstershop.CheckoutService/PlaceOrder"},
		func(ctx context.Context, _ any) (any, error) {
			return nil, telemetry.UnaryClientInterceptor()(
				ctx,
				"/hipstershop.CartService/GetCart",
				nil,
				nil,
				nil,
				func(outgoing context.Context, _ string, _, _ any, _ *grpc.ClientConn, _ ...grpc.CallOption) error {
					values, ok := metadata.FromOutgoingContext(outgoing)
					if !ok {
						t.Fatal("conventional metadata was not propagated")
					}
					if got := values.Get(conventionalTraceHeader); len(got) != 1 || got[0] != incomingTraceID {
						t.Fatalf("trace id = %v, want %q", got, incomingTraceID)
					}
					spanIDs := values.Get(conventionalSpanHeader)
					if len(spanIDs) != 1 || len(spanIDs[0]) != 16 || spanIDs[0] == incomingSpanID {
						t.Fatalf("child span id = %v, want a new 16-character id", spanIDs)
					}
					if got := values.Get("traceparent"); len(got) != 0 {
						t.Fatalf("conventional interceptor copied W3C context: %v", got)
					}
					return nil
				},
			)
		},
	)
	if err != nil {
		t.Fatalf("interceptor returned error: %v", err)
	}
}

func TestConventionalMetricsUseNoTraceIdentifiers(t *testing.T) {
	telemetry := testConventionalTelemetry()
	telemetry.finish(
		"0123456789abcdef0123456789abcdef",
		"0123456789abcdef",
		"",
		"/hipstershop.CheckoutService/PlaceOrder",
		"SERVER",
		"OK",
		fakeStartTime(),
		time.Millisecond,
	)

	response := httptest.NewRecorder()
	telemetry.serveMetrics(response, httptest.NewRequest("GET", "/metrics", nil))
	body := response.Body.String()
	if !strings.Contains(body, `service_name="checkoutservice"`) {
		t.Fatalf("metrics do not identify the service: %s", body)
	}
	if strings.Contains(body, "0123456789abcdef") || strings.Contains(body, "trace_id") || strings.Contains(body, "span_id") {
		t.Fatalf("metrics unexpectedly contain trace correlation: %s", body)
	}
}

func TestTargetServiceUsesGRPCServiceName(t *testing.T) {
	if got := targetService("/hipstershop.PaymentService/Charge"); got != "paymentservice" {
		t.Fatalf("targetService() = %q, want paymentservice", got)
	}
	if got := targetService("/hipstershop.CartService/GetCart"); got != "cartservice" {
		t.Fatalf("targetService() = %q, want cartservice", got)
	}
	if got := targetService("not-a-grpc-method"); got != "" {
		t.Fatalf("targetService() malformed method = %q, want empty", got)
	}
}

func TestConventionalInterceptorExcludesOTLPExport(t *testing.T) {
	telemetry := testConventionalTelemetry()
	invoked := false
	err := telemetry.UnaryClientInterceptor()(
		context.Background(),
		"/opentelemetry.proto.collector.trace.v1.TraceService/Export",
		nil,
		nil,
		nil,
		func(ctx context.Context, _ string, _, _ any, _ *grpc.ClientConn, _ ...grpc.CallOption) error {
			invoked = true
			if values, ok := metadata.FromOutgoingContext(ctx); ok && len(values.Get(conventionalTraceHeader)) != 0 {
				t.Fatalf("conventional context leaked into the OTLP exporter call: %v", values)
			}
			return nil
		},
	)
	if err != nil || !invoked {
		t.Fatalf("OTLP call was not passed through: invoked=%v err=%v", invoked, err)
	}
	if len(telemetry.metrics) != 0 {
		t.Fatalf("OTLP call produced conventional metrics: %v", telemetry.metrics)
	}
}

func fakeStartTime() time.Time {
	return time.Now().Add(-time.Millisecond)
}
