package main

import "testing"

func TestFrontendRouteUsesBoundedRouteNames(t *testing.T) {
	tests := map[string]string{
		"/":                 "/",
		"/cart":             "/cart",
		"/product/sku-123":  "/product/{id}",
		"/product-meta/a,b": "/product-meta/{ids}",
		"/static/app.css":   "/static/{asset}",
		"/not-a-route":      "",
	}
	for path, want := range tests {
		if got := frontendRoute(path); got != want {
			t.Errorf("frontendRoute(%q) = %q, want %q", path, got, want)
		}
	}
}
