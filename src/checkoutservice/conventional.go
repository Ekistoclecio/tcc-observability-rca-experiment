package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"sort"
	"strings"
	"sync"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

const (
	conventionalTraceHeader = "x-tcc-trace-id"
	conventionalSpanHeader  = "x-tcc-span-id"
)

type conventionalContextKey struct{}

type conventionalTraceContext struct {
	traceID string
	spanID  string
}

type conventionalMetricKey struct {
	operation string
	kind      string
	status    string
}

type conventionalMetricValue struct {
	count    uint64
	duration float64
}

type conventionalTelemetry struct {
	enabled        bool
	service        string
	zipkinEndpoint string
	client         *http.Client
	spans          chan conventionalZipkinSpan
	mu             sync.RWMutex
	metrics        map[conventionalMetricKey]conventionalMetricValue
}

type conventionalZipkinEndpoint struct {
	ServiceName string `json:"serviceName"`
}

type conventionalZipkinSpan struct {
	TraceID       string                     `json:"traceId"`
	ID            string                     `json:"id"`
	ParentID      string                     `json:"parentId,omitempty"`
	Name          string                     `json:"name"`
	Kind          string                     `json:"kind"`
	Timestamp     int64                      `json:"timestamp"`
	Duration      int64                      `json:"duration"`
	LocalEndpoint conventionalZipkinEndpoint `json:"localEndpoint"`
	Tags          map[string]string          `json:"tags,omitempty"`
}

func newConventionalTelemetry(service string) *conventionalTelemetry {
	t := &conventionalTelemetry{
		enabled:        os.Getenv("ENABLE_CONVENTIONAL_TELEMETRY") == "1",
		service:        service,
		zipkinEndpoint: os.Getenv("CONVENTIONAL_ZIPKIN_ENDPOINT"),
		client:         &http.Client{Timeout: 2 * time.Second},
		spans:          make(chan conventionalZipkinSpan, 2048),
		metrics:        make(map[conventionalMetricKey]conventionalMetricValue),
	}
	if !t.enabled {
		return t
	}
	if t.zipkinEndpoint == "" {
		t.zipkinEndpoint = "http://jaeger-a.observability-a.svc.cluster.local:9411/api/v2/spans"
	}
	go t.exportSpans()
	metricsPort := os.Getenv("CONVENTIONAL_METRICS_PORT")
	if metricsPort == "" {
		metricsPort = "9464"
	}
	metricsMux := http.NewServeMux()
	metricsMux.HandleFunc("/metrics", t.serveMetrics)
	go func() {
		_ = http.ListenAndServe(":"+metricsPort, metricsMux)
	}()
	return t
}

func randomHex(bytes int) string {
	value := make([]byte, bytes)
	if _, err := rand.Read(value); err != nil {
		panic(fmt.Sprintf("generate conventional trace identifier: %v", err))
	}
	return hex.EncodeToString(value)
}

func (t *conventionalTelemetry) UnaryServerInterceptor() grpc.UnaryServerInterceptor {
	return func(ctx context.Context, req any, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (any, error) {
		if excludedConventionalRPC(info.FullMethod) {
			return handler(ctx, req)
		}
		start := time.Now()
		requestContext := ctx
		traceID, parentID, spanID := "", "", ""
		if t.enabled {
			if incoming, ok := metadata.FromIncomingContext(ctx); ok {
				traceID = firstMetadataValue(incoming, conventionalTraceHeader)
				parentID = firstMetadataValue(incoming, conventionalSpanHeader)
			}
			if traceID == "" {
				traceID = randomHex(16)
			}
			spanID = randomHex(8)
			requestContext = context.WithValue(ctx, conventionalContextKey{}, conventionalTraceContext{traceID: traceID, spanID: spanID})
		}
		response, err := handler(requestContext, req)
		duration := time.Since(start)
		statusCode := status.Code(err).String()
		if t.enabled {
			t.finish(traceID, spanID, parentID, info.FullMethod, "SERVER", statusCode, start, duration)
		}
		return response, err
	}
}

func (t *conventionalTelemetry) UnaryClientInterceptor() grpc.UnaryClientInterceptor {
	return func(ctx context.Context, method string, req, reply any, cc *grpc.ClientConn, invoker grpc.UnaryInvoker, opts ...grpc.CallOption) error {
		if excludedConventionalRPC(method) {
			return invoker(ctx, method, req, reply, cc, opts...)
		}
		outgoing := ctx
		parent, spanID := conventionalTraceContext{}, ""
		if t.enabled {
			parent, _ = ctx.Value(conventionalContextKey{}).(conventionalTraceContext)
			if parent.traceID == "" {
				parent.traceID = randomHex(16)
			}
			spanID = randomHex(8)
			outgoing = metadata.AppendToOutgoingContext(ctx,
				conventionalTraceHeader, parent.traceID,
				conventionalSpanHeader, spanID,
			)
		}
		start := time.Now()
		err := invoker(outgoing, method, req, reply, cc, opts...)
		duration := time.Since(start)
		statusCode := status.Code(err).String()
		if t.enabled {
			t.finish(parent.traceID, spanID, parent.spanID, method, "CLIENT", statusCode, start, duration)
		}
		return err
	}
}

func targetService(method string) string {
	parts := strings.Split(strings.Trim(method, "/"), "/")
	if len(parts) != 2 {
		return ""
	}
	service := parts[0]
	if index := strings.LastIndexByte(service, '.'); index >= 0 {
		service = service[index+1:]
	}
	if service == "" {
		return ""
	}
	return strings.ToLower(service)
}

func excludedConventionalRPC(method string) bool {
	return strings.Contains(method, "grpc.health") ||
		strings.Contains(method, "opentelemetry.proto.collector")
}

func firstMetadataValue(values metadata.MD, key string) string {
	items := values.Get(key)
	if len(items) == 0 {
		return ""
	}
	return items[0]
}

func (t *conventionalTelemetry) finish(traceID, spanID, parentID, operation, kind, result string, start time.Time, duration time.Duration) {
	key := conventionalMetricKey{operation: operation, kind: strings.ToLower(kind), status: result}
	t.mu.Lock()
	metric := t.metrics[key]
	metric.count++
	metric.duration += duration.Seconds()
	t.metrics[key] = metric
	t.mu.Unlock()
	span := conventionalZipkinSpan{
		TraceID:       traceID,
		ID:            spanID,
		ParentID:      parentID,
		Name:          operation,
		Kind:          kind,
		Timestamp:     start.UnixMicro(),
		Duration:      duration.Microseconds(),
		LocalEndpoint: conventionalZipkinEndpoint{ServiceName: t.service},
		Tags:          map[string]string{"result": result},
	}
	select {
	case t.spans <- span:
	default:
	}
}

func (t *conventionalTelemetry) exportSpans() {
	for span := range t.spans {
		payload, err := json.Marshal([]conventionalZipkinSpan{span})
		if err != nil {
			continue
		}
		req, err := http.NewRequest(http.MethodPost, t.zipkinEndpoint, strings.NewReader(string(payload)))
		if err != nil {
			continue
		}
		req.Header.Set("Content-Type", "application/json")
		resp, err := t.client.Do(req)
		if err == nil {
			_ = resp.Body.Close()
		}
	}
}

func (t *conventionalTelemetry) serveMetrics(w http.ResponseWriter, _ *http.Request) {
	t.mu.RLock()
	keys := make([]conventionalMetricKey, 0, len(t.metrics))
	for key := range t.metrics {
		keys = append(keys, key)
	}
	sort.Slice(keys, func(i, j int) bool {
		return keys[i].operation+keys[i].kind+keys[i].status < keys[j].operation+keys[j].kind+keys[j].status
	})
	values := make(map[conventionalMetricKey]conventionalMetricValue, len(t.metrics))
	for key, value := range t.metrics {
		values[key] = value
	}
	t.mu.RUnlock()
	w.Header().Set("Content-Type", "text/plain; version=0.0.4")
	fmt.Fprintln(w, "# HELP tcc_conventional_requests_total Completed conventional requests and RPCs.")
	fmt.Fprintln(w, "# TYPE tcc_conventional_requests_total counter")
	for _, key := range keys {
		labels := t.metricLabels(key)
		fmt.Fprintf(w, "tcc_conventional_requests_total{%s} %d\n", labels, values[key].count)
	}
	fmt.Fprintln(w, "# HELP tcc_conventional_request_duration_seconds_sum Total conventional request and RPC duration.")
	fmt.Fprintln(w, "# TYPE tcc_conventional_request_duration_seconds_sum counter")
	for _, key := range keys {
		labels := t.metricLabels(key)
		fmt.Fprintf(w, "tcc_conventional_request_duration_seconds_sum{%s} %g\n", labels, values[key].duration)
		fmt.Fprintf(w, "tcc_conventional_request_duration_seconds_count{%s} %d\n", labels, values[key].count)
	}
}

func (t *conventionalTelemetry) metricLabels(key conventionalMetricKey) string {
	return fmt.Sprintf("service_name=%q,operation=%q,span_kind=%q,status_code=%q",
		t.service, key.operation, key.kind, key.status)
}
