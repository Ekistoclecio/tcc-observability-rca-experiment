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

type conventionalServerMetric struct {
	operation string
	status    string
}

type conventionalServerTelemetry struct {
	enabled        bool
	service        string
	zipkinEndpoint string
	client         *http.Client
	mu             sync.RWMutex
	count          map[conventionalServerMetric]uint64
	duration       map[conventionalServerMetric]float64
}

type conventionalServerZipkinSpan struct {
	TraceID       string `json:"traceId"`
	ID            string `json:"id"`
	ParentID      string `json:"parentId,omitempty"`
	Name          string `json:"name"`
	Kind          string `json:"kind"`
	Timestamp     int64  `json:"timestamp"`
	Duration      int64  `json:"duration"`
	LocalEndpoint struct {
		ServiceName string `json:"serviceName"`
	} `json:"localEndpoint"`
	Tags map[string]string `json:"tags"`
}

func newConventionalServerTelemetry(service string) *conventionalServerTelemetry {
	t := &conventionalServerTelemetry{
		enabled:        os.Getenv("ENABLE_CONVENTIONAL_TELEMETRY") == "1",
		service:        service,
		zipkinEndpoint: os.Getenv("CONVENTIONAL_ZIPKIN_ENDPOINT"),
		client:         &http.Client{Timeout: 2 * time.Second},
		count:          make(map[conventionalServerMetric]uint64),
		duration:       make(map[conventionalServerMetric]float64),
	}
	if !t.enabled {
		return t
	}
	if t.zipkinEndpoint == "" {
		t.zipkinEndpoint = "http://jaeger-a.observability-a.svc.cluster.local:9411/api/v2/spans"
	}
	port := os.Getenv("CONVENTIONAL_METRICS_PORT")
	if port == "" {
		port = "9464"
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/metrics", t.serveMetrics)
	go func() { _ = http.ListenAndServe(":"+port, mux) }()
	return t
}

func (t *conventionalServerTelemetry) UnaryServerInterceptor() grpc.UnaryServerInterceptor {
	return func(ctx context.Context, req any, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (any, error) {
		if strings.Contains(info.FullMethod, "grpc.health") {
			return handler(ctx, req)
		}
		traceID, parentID, spanID := "", "", ""
		if t.enabled {
			traceID, parentID = conventionalIncomingIDs(ctx)
			if traceID == "" {
				traceID = conventionalRandomHex(16)
			}
			spanID = conventionalRandomHex(8)
		}
		start := time.Now()
		response, err := handler(ctx, req)
		duration := time.Since(start)
		result := status.Code(err).String()
		if t.enabled {
			t.finish(traceID, spanID, parentID, info.FullMethod, result, start, duration)
		}
		return response, err
	}
}

func conventionalIncomingIDs(ctx context.Context) (string, string) {
	values, ok := metadata.FromIncomingContext(ctx)
	if !ok {
		return "", ""
	}
	var traceID, parentID string
	if ids := values.Get(conventionalTraceHeader); len(ids) != 0 {
		traceID = ids[0]
	}
	if ids := values.Get(conventionalSpanHeader); len(ids) != 0 {
		parentID = ids[0]
	}
	return traceID, parentID
}

func conventionalRandomHex(size int) string {
	value := make([]byte, size)
	if _, err := rand.Read(value); err != nil {
		panic(fmt.Sprintf("generate conventional trace identifier: %v", err))
	}
	return hex.EncodeToString(value)
}

func (t *conventionalServerTelemetry) finish(traceID, spanID, parentID, operation, result string, start time.Time, duration time.Duration) {
	key := conventionalServerMetric{operation: operation, status: result}
	t.mu.Lock()
	t.count[key]++
	t.duration[key] += duration.Seconds()
	t.mu.Unlock()
	span := conventionalServerZipkinSpan{
		TraceID: traceID, ID: spanID, ParentID: parentID, Name: operation, Kind: "SERVER",
		Timestamp: start.UnixMicro(), Duration: duration.Microseconds(),
		Tags: map[string]string{"result": result},
	}
	span.LocalEndpoint.ServiceName = t.service
	go t.export(span)
}

func (t *conventionalServerTelemetry) export(span conventionalServerZipkinSpan) {
	payload, err := json.Marshal([]conventionalServerZipkinSpan{span})
	if err != nil {
		return
	}
	request, err := http.NewRequest(http.MethodPost, t.zipkinEndpoint, strings.NewReader(string(payload)))
	if err != nil {
		return
	}
	request.Header.Set("Content-Type", "application/json")
	response, err := t.client.Do(request)
	if err == nil {
		_ = response.Body.Close()
	}
}

func (t *conventionalServerTelemetry) serveMetrics(w http.ResponseWriter, _ *http.Request) {
	t.mu.RLock()
	keys := make([]conventionalServerMetric, 0, len(t.count))
	for key := range t.count {
		keys = append(keys, key)
	}
	sort.Slice(keys, func(i, j int) bool { return keys[i].operation+keys[i].status < keys[j].operation+keys[j].status })
	counts := make(map[conventionalServerMetric]uint64, len(keys))
	durations := make(map[conventionalServerMetric]float64, len(keys))
	for _, key := range keys {
		counts[key], durations[key] = t.count[key], t.duration[key]
	}
	t.mu.RUnlock()
	w.Header().Set("Content-Type", "text/plain; version=0.0.4")
	fmt.Fprintln(w, "# HELP tcc_conventional_requests_total Completed conventional requests and RPCs.")
	fmt.Fprintln(w, "# TYPE tcc_conventional_requests_total counter")
	for _, key := range keys {
		labels := fmt.Sprintf("service_name=%q,operation=%q,span_kind=\"server\",status_code=%q", t.service, key.operation, key.status)
		fmt.Fprintf(w, "tcc_conventional_requests_total{%s} %d\n", labels, counts[key])
		fmt.Fprintf(w, "tcc_conventional_request_duration_seconds_sum{%s} %g\n", labels, durations[key])
		fmt.Fprintf(w, "tcc_conventional_request_duration_seconds_count{%s} %d\n", labels, counts[key])
	}
}
