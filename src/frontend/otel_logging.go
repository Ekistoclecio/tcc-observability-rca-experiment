package main

import (
	"context"

	"github.com/sirupsen/logrus"
	"go.opentelemetry.io/contrib/bridges/otellogrus"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlplog/otlploggrpc"
	sdklog "go.opentelemetry.io/otel/sdk/log"
	"go.opentelemetry.io/otel/sdk/resource"
)

func configureOtelLogging(ctx context.Context, collectorAddr, service string, logger *logrus.Logger) error {
	exporter, err := otlploggrpc.New(ctx,
		otlploggrpc.WithEndpoint(collectorAddr),
		otlploggrpc.WithInsecure(),
	)
	if err != nil {
		return err
	}
	provider := sdklog.NewLoggerProvider(
		sdklog.WithProcessor(sdklog.NewBatchProcessor(exporter)),
		sdklog.WithResource(resource.NewWithAttributes("", attribute.String("service.name", service))),
	)
	logger.AddHook(otellogrus.NewHook(service, otellogrus.WithLoggerProvider(provider)))
	return nil
}
