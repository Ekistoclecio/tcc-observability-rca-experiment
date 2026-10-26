import sys
import types
import unittest
from collections import namedtuple

try:
    import grpc  # noqa: F401
except ModuleNotFoundError:
    grpc = types.ModuleType("grpc")
    grpc.ServerInterceptor = type("ServerInterceptor", (), {})
    grpc.UnaryUnaryClientInterceptor = type("UnaryUnaryClientInterceptor", (), {})
    grpc.ClientCallDetails = type("ClientCallDetails", (), {})
    grpc.StatusCode = types.SimpleNamespace(OK=object())
    grpc.unary_unary_rpc_method_handler = lambda *args, **kwargs: None
    sys.modules["grpc"] = grpc

from conventional_telemetry import extract_context


Metadata = namedtuple("Metadata", ("key", "value"))


class ConventionalContextTest(unittest.TestCase):
    def test_extracts_only_custom_context(self):
        metadata = (
            Metadata("x-tcc-trace-id", "a" * 32),
            Metadata("x-tcc-span-id", "b" * 16),
            Metadata("traceparent", f"00-{'c' * 32}-{'d' * 16}-01"),
        )
        self.assertEqual(extract_context(metadata), ("a" * 32, "b" * 16))

    def test_does_not_derive_context_from_w3c_headers(self):
        metadata = (Metadata("traceparent", f"00-{'c' * 32}-{'d' * 16}-01"),)
        self.assertEqual(extract_context(metadata), (None, None))


if __name__ == "__main__":
    unittest.main()
