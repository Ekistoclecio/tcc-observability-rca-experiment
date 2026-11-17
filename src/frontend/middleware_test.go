package main

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/sirupsen/logrus"
	"go.opentelemetry.io/contrib/bridges/otellogrus"
	sdklog "go.opentelemetry.io/otel/sdk/log"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
)

type capturedLogCorrelation struct {
	traceID trace.TraceID
	spanID  trace.SpanID
}

type correlationExporter struct {
	records []capturedLogCorrelation
}

func (e *correlationExporter) Export(_ context.Context, records []sdklog.Record) error {
	for _, record := range records {
		e.records = append(e.records, capturedLogCorrelation{
			traceID: record.TraceID(),
			spanID:  record.SpanID(),
		})
	}
	return nil
}

func (*correlationExporter) Shutdown(context.Context) error   { return nil }
func (*correlationExporter) ForceFlush(context.Context) error { return nil }

func loggerWithOTelCapture(t *testing.T) (*logrus.Logger, *correlationExporter) {
	t.Helper()

	exporter := &correlationExporter{}
	provider := sdklog.NewLoggerProvider(
		sdklog.WithProcessor(sdklog.NewSimpleProcessor(exporter)),
	)
	t.Cleanup(func() {
		if err := provider.Shutdown(context.Background()); err != nil {
			t.Errorf("shutdown test log provider: %v", err)
		}
	})

	logger := logrus.New()
	logger.SetOutput(io.Discard)
	logger.AddHook(otellogrus.NewHook("frontend-test", otellogrus.WithLoggerProvider(provider)))
	return logger, exporter
}

func TestRequestLoggerUsesCurrentActiveSpanContext(t *testing.T) {
	logger, exporter := loggerWithOTelCapture(t)
	traceProvider := sdktrace.NewTracerProvider()
	t.Cleanup(func() {
		if err := traceProvider.Shutdown(context.Background()); err != nil {
			t.Errorf("shutdown test trace provider: %v", err)
		}
	})

	requestContext, span := traceProvider.Tracer("frontend-test").Start(context.Background(), "POST /cart/checkout")
	defer span.End()

	// The middleware-created Entry has the pre-span context. The handler's
	// current request context contains the active SERVER span.
	entry := logger.WithContext(context.Background()).WithField("http.req.path", "/cart/checkout")
	requestContext = context.WithValue(requestContext, ctxKeyLog{}, entry)
	request := httptest.NewRequest(http.MethodPost, "/cart/checkout", nil).WithContext(requestContext)

	requestLogger(request).Info("order placed")

	if len(exporter.records) != 1 {
		t.Fatalf("exported records = %d, want 1", len(exporter.records))
	}
	got := exporter.records[0]
	want := span.SpanContext()
	if got.traceID != want.TraceID() {
		t.Errorf("record trace ID = %s, want active trace ID %s", got.traceID, want.TraceID())
	}
	if got.spanID != want.SpanID() {
		t.Errorf("record span ID = %s, want active span ID %s", got.spanID, want.SpanID())
	}
}

func TestRequestLoggerDoesNotFabricateCorrelationWithoutActiveSpan(t *testing.T) {
	logger, exporter := loggerWithOTelCapture(t)
	entry := logger.WithContext(context.Background())
	requestContext := context.WithValue(context.Background(), ctxKeyLog{}, entry)
	request := httptest.NewRequest(http.MethodGet, "/_healthz", nil).WithContext(requestContext)

	requestLogger(request).Info("request without active span")

	if len(exporter.records) != 1 {
		t.Fatalf("exported records = %d, want 1", len(exporter.records))
	}
	got := exporter.records[0]
	if got.traceID.IsValid() {
		t.Errorf("record trace ID = %s, want no trace ID", got.traceID)
	}
	if got.spanID.IsValid() {
		t.Errorf("record span ID = %s, want no span ID", got.spanID)
	}
}
