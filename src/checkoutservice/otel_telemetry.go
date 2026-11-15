// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//      https://www.apache.org/licenses/LICENSE-2.0

package main

import (
	"context"
	"time"

	"github.com/sirupsen/logrus"
	"go.opentelemetry.io/contrib/bridges/otellogrus"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	otelmetric "go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/exporters/otlp/otlplog/otlploggrpc"
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetricgrpc"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
	sdklog "go.opentelemetry.io/otel/sdk/log"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"google.golang.org/grpc"
)

var (
	sharedRPCServerRequests  otelmetric.Int64Counter
	sharedRPCServerDuration  otelmetric.Float64Histogram
	sharedRPCClientRequests  otelmetric.Int64Counter
	sharedRPCClientDuration  otelmetric.Float64Histogram
)

func configureOTelProviders(ctx context.Context, serviceName string, conn *grpc.ClientConn, logger *logrus.Logger) (*sdktrace.TracerProvider, error) {
	res, err := resource.Merge(resource.Default(), resource.NewSchemaless(
		attribute.String("service.name", serviceName),
		attribute.String("service.version", "0.10.7"),
		attribute.String("deployment.environment.name", "tcc-local"),
	))
	if err != nil {
		return nil, err
	}
	traceExporter, err := otlptracegrpc.New(ctx, otlptracegrpc.WithGRPCConn(conn))
	if err != nil {
		return nil, err
	}
	tp := sdktrace.NewTracerProvider(sdktrace.WithBatcher(traceExporter), sdktrace.WithSampler(sdktrace.AlwaysSample()), sdktrace.WithResource(res))
	otel.SetTracerProvider(tp)
	metricExporter, err := otlpmetricgrpc.New(ctx, otlpmetricgrpc.WithGRPCConn(conn))
	if err != nil {
		return nil, err
	}
	meterProvider := sdkmetric.NewMeterProvider(sdkmetric.WithResource(res), sdkmetric.WithReader(sdkmetric.NewPeriodicReader(metricExporter, sdkmetric.WithInterval(5*time.Second))))
	otel.SetMeterProvider(meterProvider)
	sharedMeter := meterProvider.Meter("tcc/shared-observation")
	sharedRPCServerRequests, err = sharedMeter.Int64Counter("tcc.otel.rpc.server.requests", otelmetric.WithUnit("{request}"))
	if err != nil {
		return nil, err
	}
	sharedRPCServerDuration, err = sharedMeter.Float64Histogram("tcc.otel.rpc.server.duration", otelmetric.WithUnit("s"))
	if err != nil {
		return nil, err
	}
	sharedRPCClientRequests, err = sharedMeter.Int64Counter("tcc.otel.rpc.client.requests", otelmetric.WithUnit("{request}"))
	if err != nil {
		return nil, err
	}
	sharedRPCClientDuration, err = sharedMeter.Float64Histogram("tcc.otel.rpc.client.duration", otelmetric.WithUnit("s"))
	if err != nil {
		return nil, err
	}
	logExporter, err := otlploggrpc.New(ctx, otlploggrpc.WithGRPCConn(conn))
	if err != nil {
		return nil, err
	}
	lp := sdklog.NewLoggerProvider(sdklog.WithResource(res), sdklog.WithProcessor(sdklog.NewBatchProcessor(logExporter)))
	logger.AddHook(otellogrus.NewHook(serviceName, otellogrus.WithLoggerProvider(lp)))
	return tp, nil
}

func recordSharedRPCObservation(ctx context.Context, kind, operation, target, statusCode string, duration time.Duration) {
	if statusCode == "" {
		statusCode = "UNKNOWN"
	}
	outcome := "SUCCESS"
	if statusCode != "OK" {
		outcome = "ERROR"
	}
	attrs := []attribute.KeyValue{
		attribute.String("rpc.system.name", "grpc"),
		attribute.String("rpc.method", operation),
		attribute.String("kind", kind),
		attribute.String("outcome", outcome),
		attribute.String("rpc.response.status_code", statusCode),
	}
	if target != "" {
		attrs = append(attrs, attribute.String("target_service", target))
	}
	options := otelmetric.WithAttributes(attrs...)
	if kind == "CLIENT" {
		if sharedRPCClientRequests != nil && sharedRPCClientDuration != nil {
			sharedRPCClientRequests.Add(ctx, 1, options)
			sharedRPCClientDuration.Record(ctx, duration.Seconds(), options)
		}
		return
	}
	if sharedRPCServerRequests != nil && sharedRPCServerDuration != nil {
		sharedRPCServerRequests.Add(ctx, 1, options)
		sharedRPCServerDuration.Record(ctx, duration.Seconds(), options)
	}
}
