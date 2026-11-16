package main

import (
	"context"
	"testing"

	"google.golang.org/grpc/metadata"
)

func TestConventionalIncomingIDsIgnoreW3CContext(t *testing.T) {
	ctx := metadata.NewIncomingContext(context.Background(), metadata.Pairs(
		conventionalTraceHeader, "0123456789abcdef0123456789abcdef",
		conventionalSpanHeader, "0123456789abcdef",
		"traceparent", "00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01",
	))
	traceID, parentID := conventionalIncomingIDs(ctx)
	if traceID != "0123456789abcdef0123456789abcdef" {
		t.Fatalf("trace id = %q", traceID)
	}
	if parentID != "0123456789abcdef" {
		t.Fatalf("parent id = %q", parentID)
	}

	w3cOnly := metadata.NewIncomingContext(context.Background(), metadata.Pairs(
		"traceparent", "00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01",
	))
	traceID, parentID = conventionalIncomingIDs(w3cOnly)
	if traceID != "" || parentID != "" {
		t.Fatalf("W3C context contaminated conventional context: trace=%q parent=%q", traceID, parentID)
	}
}
