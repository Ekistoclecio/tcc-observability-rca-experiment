from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
from dotenv import dotenv_values

from scripts.experiment.configuration import (
    ConfigurationError, FAULT_VARIABLES, ROOT, configuration_plan, resolve_config,
)

CONTROLLED_ENV = {name for name, _ in FAULT_VARIABLES.values()} | {
    "OPENAI_API_KEY", "RCA_ENABLED", "RCA_MODEL", "RCA_REASONING_MODE",
    "RCA_REASONING_EFFORT", "RCA_MAX_OUTPUT_TOKENS", "RCA_SERVICE_TIER",
    "EVALUATION_ENABLED",
}


def values(**overrides: str) -> dict[str, str]:
    result = {variable: "1" for variable, _ in FAULT_VARIABLES.values()}
    result.update({"OPENAI_API_KEY": "synthetic-test-secret", "RCA_ENABLED": "true",
                   "EVALUATION_ENABLED": "true"})
    result.update(overrides)
    return result


class ConfigurationTests(unittest.TestCase):
    def resolve(self, env: dict[str, str]):
        return resolve_config(ROOT, ROOT / ".env", env)

    def test_defaults_match_env_example_and_pilot_plan(self):
        env = {key: str(value) for key, value in dotenv_values(ROOT / ".env.example").items()
               if value is not None}
        env["OPENAI_API_KEY"] = "synthetic-test-secret"
        config = self.resolve(env)
        self.assertEqual(config.fault_repetitions, {f"F{i}": 1 for i in range(1, 6)})
        self.assertEqual((config.physical_fault_instances, config.treatment_cases,
                          config.expected_rca_inputs), (5, 10, 10))
        self.assertEqual(config.rca.model, "gpt-6-luna")
        self.assertEqual(config.rca.reasoning_mode, "standard")
        self.assertEqual(config.rca.reasoning_effort, "medium")
        self.assertEqual(config.rca.max_output_tokens, 16384)
        self.assertEqual(config.rca.service_tier, "default")

    def test_zero_disabled_multiple_and_mixed_plans(self):
        env = values(EXPERIMENT_F1_CPU_SATURATION="3", EXPERIMENT_F2_NETWORK_LATENCY="0",
                     EXPERIMENT_F3_SERVICE_UNAVAILABLE="2", EXPERIMENT_F4_APPLICATION_EXCEPTION="1",
                     EXPERIMENT_F5_DATASTORE_UNAVAILABLE="4")
        config = self.resolve(env)
        self.assertEqual(config.instances, ["F1-R01", "F1-R02", "F1-R03", "F3-R01",
                                             "F3-R02", "F4-R01", "F5-R01", "F5-R02",
                                             "F5-R03", "F5-R04"])
        self.assertEqual((config.physical_fault_instances, config.treatment_cases), (10, 20))
        self.assertEqual(configuration_plan(config)["expected_rca_inputs"], 20)

    def test_all_five_by_five_plan(self):
        env = values(**{variable: "5" for variable, _ in FAULT_VARIABLES.values()})
        config = self.resolve(env)
        self.assertEqual((config.physical_fault_instances, config.treatment_cases,
                          config.expected_rca_inputs), (25, 50, 50))

    def test_all_zero_fails_before_execution(self):
        env = values(**{variable: "0" for variable, _ in FAULT_VARIABLES.values()})
        with self.assertRaisesRegex(ConfigurationError, "No fault repetitions configured"):
            self.resolve(env)

    def test_invalid_repetition_values_name_the_variable(self):
        for invalid in ("-1", "1.5", "abc", ""):
            with self.subTest(value=invalid):
                with self.assertRaisesRegex(ConfigurationError, "EXPERIMENT_F2_NETWORK_LATENCY"):
                    self.resolve(values(EXPERIMENT_F2_NETWORK_LATENCY=invalid))

    def test_strict_booleans_and_evaluation_dependency(self):
        with self.assertRaisesRegex(ConfigurationError, "RCA_ENABLED must be true or false"):
            self.resolve(values(RCA_ENABLED="yes"))
        with self.assertRaisesRegex(ConfigurationError, "requires RCA_ENABLED"):
            self.resolve(values(RCA_ENABLED="false", EVALUATION_ENABLED="true"))
        config = self.resolve(values(RCA_ENABLED="false", EVALUATION_ENABLED="false",
                                    OPENAI_API_KEY=""))
        self.assertFalse(config.rca.enabled)
        self.assertFalse(config.rca.api_key_available)
        self.assertEqual(config.expected_rca_inputs, config.treatment_cases)

    def test_invalid_rca_settings_fail_fast(self):
        cases = (
            ("RCA_MODEL", "unknown-model"),
            ("RCA_REASONING_MODE", "arbitrary"),
            ("RCA_REASONING_EFFORT", "extreme"),
            ("RCA_MAX_OUTPUT_TOKENS", "0"),
            ("RCA_MAX_OUTPUT_TOKENS", "1.5"),
            ("RCA_SERVICE_TIER", "flex"),
        )
        for key, value in cases:
            with self.subTest(key=key, value=value), self.assertRaises(ConfigurationError):
                self.resolve(values(**{key: value}))

    def test_api_key_required_only_when_rca_enabled(self):
        with self.assertRaisesRegex(ConfigurationError, "OPENAI_API_KEY is required"):
            self.resolve(values(OPENAI_API_KEY=""))
        self.assertFalse(self.resolve(values(RCA_ENABLED="false", EVALUATION_ENABLED="false",
                                             OPENAI_API_KEY="")).rca.api_key_available)

    def test_secret_never_enters_configuration_or_manifest(self):
        secret = "synthetic-never-persist-me"
        config = self.resolve(values(OPENAI_API_KEY=secret))
        serialized = repr(config) + repr(config.manifest_configuration()) + repr(configuration_plan(config))
        self.assertNotIn(secret, serialized)
        self.assertNotIn("OPENAI_API_KEY", repr(config.manifest_configuration()))

    def test_root_script_dry_run_from_another_directory(self):
        secret = "synthetic-dry-run-key"
        with tempfile.NamedTemporaryFile("w", suffix=".env", prefix=".offline-config-",
                                         dir=ROOT, encoding="utf-8", delete=False) as stream:
            stream.write("\n".join([*(f"{key}={value}" for key, value in values().items()), ""]))
            selected = Path(stream.name)
        try:
            environment = {key: value for key, value in __import__("os").environ.items()
                           if key not in CONTROLLED_ENV}
            environment["OPENAI_API_KEY"] = secret
            environment["EXPERIMENT_F1_CPU_SATURATION"] = "2"
            result = subprocess.run([str(ROOT / "run-experiment.sh"), "--dry-run",
                                     "--env-file", str(selected)], cwd=ROOT / "docs",
                                    env=environment, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("F1 CPU saturation: 2", result.stdout)
            self.assertIn("Physical fault instances: 6", result.stdout)
            self.assertIn("Expected RCA inputs:       12", result.stdout)
            self.assertIn("DRY-RUN", result.stdout)
            self.assertNotIn(secret, result.stdout + result.stderr)
            self.assertTrue((ROOT / "run-experiment.sh").stat().st_mode & 0o111)
        finally:
            selected.unlink(missing_ok=True)

    def test_root_dry_run_prints_full_and_mixed_plans_without_side_effects(self):
        configs = [
            ({variable: "5" for variable, _ in FAULT_VARIABLES.values()},
             "Physical fault instances: 25", "Expected RCA inputs:       50"),
            ({"EXPERIMENT_F1_CPU_SATURATION": "3",
              "EXPERIMENT_F2_NETWORK_LATENCY": "0",
              "EXPERIMENT_F3_SERVICE_UNAVAILABLE": "2",
              "EXPERIMENT_F4_APPLICATION_EXCEPTION": "1",
              "EXPERIMENT_F5_DATASTORE_UNAVAILABLE": "4"},
             "Physical fault instances: 10", "Expected RCA inputs:       20"),
        ]
        for counts, physical, inputs in configs:
            with self.subTest(physical=physical):
                environment_lines = [f"{key}={value}" for key, value in values().items()]
                environment_lines.extend(f"{key}={value}" for key, value in counts.items())
                with tempfile.NamedTemporaryFile("w", suffix=".env", prefix=".offline-plan-",
                                                 dir=ROOT, encoding="utf-8", delete=False) as stream:
                    stream.write("\n".join(environment_lines) + "\n")
                    selected = Path(stream.name)
                try:
                    env = {key: value for key, value in __import__("os").environ.items()
                           if key not in CONTROLLED_ENV}
                    result = subprocess.run([str(ROOT / "run-experiment.sh"), "--dry-run",
                                             "--env-file", str(selected)], cwd=ROOT / "docs",
                                            env=env, capture_output=True, text=True, check=False)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(physical, result.stdout)
                    self.assertIn(inputs, result.stdout)
                    self.assertIn("DRY-RUN", result.stdout)
                finally:
                    selected.unlink(missing_ok=True)

    def test_root_script_propagates_configuration_failure(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", prefix=".offline-invalid-",
                                         dir=ROOT, encoding="utf-8", delete=False) as stream:
            stream.write("EXPERIMENT_F1_CPU_SATURATION=1\n"
                         "EXPERIMENT_F2_NETWORK_LATENCY=not-an-int\n"
                         "OPENAI_API_KEY=synthetic-only\n")
            selected = Path(stream.name)
        try:
            result = subprocess.run([str(ROOT / "run-experiment.sh"), "--dry-run",
                                     "--env-file", str(selected)], cwd=ROOT / "docs",
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 2)
            self.assertIn("EXPERIMENT_F2_NETWORK_LATENCY", result.stderr)
            self.assertNotIn("synthetic-only", result.stdout + result.stderr)
        finally:
            selected.unlink(missing_ok=True)

    def test_missing_environment_file_fails_before_side_effects(self):
        from scripts.experiment.cli import run_experiment
        missing = ROOT / ".does-not-exist-test.env"
        with self.assertRaisesRegex(ConfigurationError, "environment file not found"):
            run_experiment(missing, dry_run=True)

    def test_missing_api_key_fails_before_kind_preflight(self):
        import os
        from unittest.mock import patch
        from scripts.experiment import cli

        with tempfile.NamedTemporaryFile("w", suffix=".env", prefix=".offline-no-key-",
                                         dir=ROOT, encoding="utf-8", delete=False) as stream:
            stream.write("EXPERIMENT_F1_CPU_SATURATION=1\n"
                         "OPENAI_API_KEY=\nRCA_ENABLED=true\nEVALUATION_ENABLED=true\n")
            selected = Path(stream.name)
        try:
            with patch.dict(os.environ, {"OPENAI_API_KEY": ""}), \
                 patch.object(cli, "_active_kind_clusters") as kind_check:
                with self.assertRaisesRegex(ConfigurationError, "OPENAI_API_KEY is required"):
                    cli.run_experiment(selected, dry_run=False)
                kind_check.assert_not_called()
        finally:
            selected.unlink(missing_ok=True)

    def test_root_dry_run_allows_disabled_rca_without_key(self):
        content = values(EXPERIMENT_F1_CPU_SATURATION="1", EXPERIMENT_F2_NETWORK_LATENCY="0",
                         EXPERIMENT_F3_SERVICE_UNAVAILABLE="0",
                         EXPERIMENT_F4_APPLICATION_EXCEPTION="0",
                         EXPERIMENT_F5_DATASTORE_UNAVAILABLE="0",
                         RCA_ENABLED="false", EVALUATION_ENABLED="false", OPENAI_API_KEY="")
        with tempfile.NamedTemporaryFile("w", suffix=".env", prefix=".offline-rca-disabled-",
                                         dir=ROOT, encoding="utf-8", delete=False) as stream:
            stream.write("\n".join(f"{key}={value}" for key, value in content.items()) + "\n")
            selected = Path(stream.name)
        try:
            import os
            environment = {key: value for key, value in os.environ.items()
                           if key not in CONTROLLED_ENV}
            result = subprocess.run([str(ROOT / "run-experiment.sh"), "--dry-run",
                                     "--env-file", str(selected)], cwd=ROOT / "docs",
                                    env=environment, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Physical fault instances: 1", result.stdout)
            self.assertIn("Enabled:                   no", result.stdout)
            self.assertIn("Expected RCA inputs:       2", result.stdout)
        finally:
            selected.unlink(missing_ok=True)

    def test_reproducibility_readme_references_real_entrypoint_and_pins(self):
        readme = ROOT / "docs/experiment/README.md"
        content = readme.read_text(encoding="utf-8")
        self.assertTrue((ROOT / "run-experiment.sh").is_file())
        self.assertTrue((ROOT / ".env.example").is_file())
        self.assertIn("./run-experiment.sh", content)
        self.assertIn("docs/experiment/results/run-XXX/", content)
        versions = (ROOT / "config/experiment/environment/versions.env").read_text(encoding="utf-8")
        for pin in ("v0.33.0", "v1.35.8", "29.9.2", "3.17.3", "2.8.6"):
            self.assertIn(pin, content)
            self.assertIn(pin, versions)


if __name__ == "__main__":
    unittest.main()
