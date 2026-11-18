from __future__ import annotations

from unittest import TestCase
from unittest.mock import patch

from scripts.experiment import prerequisites


class RuntimeDependencyTests(TestCase):
    def test_reports_all_missing_runtime_commands_at_once(self):
        with patch.object(prerequisites, "_resolve_command", return_value=None), \
             patch.object(prerequisites, "check_python_dependencies"):
            with self.assertRaises(prerequisites.DependencyPreflightError) as caught:
                prerequisites.check_runtime_dependencies(
                    {"PATH": "/empty", "KIND_BIN": "kind", "KUBECTL_BIN": "kubectl"},
                    {"KIND_VERSION": "v0.33.0", "KUBECTL_VERSION": "v1.35.8",
                     "DOCKER_SERVER_VERSION": "29.9.2", "HELM_VERSION": "3.17.3"},
                )

        message = str(caught.exception)
        for dependency in ("Kind", "kubectl", "Docker CLI/Engine", "Python runtime",
                           "curl", "Git", "Bash"):
            self.assertIn(dependency, message)
        self.assertIn("v0.33.0", message)
        self.assertIn("v1.35.8", message)

    def test_reports_docker_daemon_failure_explicitly(self):
        def resolve(command: str, _path: str | None) -> str:
            return f"/bin/{command}"

        def probe(argv: list[str], _env):
            if argv[0].endswith("kind"):
                return "kind v0.33.0"
            if argv[0].endswith("kubectl"):
                return '{"clientVersion":{"gitVersion":"v1.35.8"}}'
            if argv[:3] == ["docker", "version", "--format"]:
                return "29.9.2"
            if argv[:2] == ["docker", "info"]:
                return None
            if argv[0] == "python3":
                return "Python 3.12.3"
            return ""

        def run_version(argv, _env, timeout=10):
            output = probe(argv, _env)
            if output is None:
                return None, "exit 1"
            return output, None

        with patch.object(prerequisites, "_resolve_command", side_effect=resolve), \
             patch.object(prerequisites, "_run_version", side_effect=run_version), \
             patch.object(prerequisites, "check_python_dependencies"):
            with self.assertRaises(prerequisites.DependencyPreflightError) as caught:
                prerequisites.check_runtime_dependencies(
                    {"PATH": "/bin", "KIND_BIN": "kind", "KUBECTL_BIN": "kubectl"},
                    {"KIND_VERSION": "v0.33.0", "KUBECTL_VERSION": "v1.35.8",
                     "DOCKER_SERVER_VERSION": "29.9.2", "HELM_VERSION": "3.17.3"},
                )
        self.assertIn("Docker daemon", str(caught.exception))

    def test_python_dependency_preflight_lists_missing_pins(self):
        with patch.object(prerequisites, "_python_packages",
                          return_value=[("openai", "3.51.0"), ("jsonschema", "4.25.1")]), \
             patch.object(prerequisites.importlib.metadata, "version",
                          side_effect=prerequisites.importlib.metadata.PackageNotFoundError):
            with self.assertRaises(prerequisites.DependencyPreflightError) as caught:
                prerequisites.check_python_dependencies()
        self.assertIn("openai", str(caught.exception))
        self.assertIn("jsonschema", str(caught.exception))


if __name__ == "__main__":
    import unittest
    unittest.main()
