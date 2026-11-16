from __future__ import annotations

import unittest

from scripts.experiment.validate_deployment_provenance import (
    SERVICES,
    evaluate_service,
    normalize_config_digest,
    validate_records,
)


REVISION = "d3670c83cf4f49764099e6f251bc238011595b62"
CONFIG_ID = "sha256:" + "a" * 64


def service_record(service="paymentservice", **overrides):
    values = {
        "service": service,
        "expected_revision": REVISION,
        "image_reference": f"paymentservice:tcc-{REVISION}",
        "local_image_id": "sha256:" + "b" * 64,
        "local_image_config_id": CONFIG_ID,
        "image_revision": REVISION,
        "config_revision": REVISION,
        "deployment_image": f"paymentservice:tcc-{REVISION}",
        "desired_replicas": 1,
        "ready_replicas": 1,
        "pods": [{
            "name": "paymentservice-abc",
            "container": "server",
            "image": f"paymentservice:tcc-{REVISION}",
            "image_id": "containerd://" + CONFIG_ID,
            "ready": True,
        }],
    }
    values.update(overrides)
    return evaluate_service(**values)


class DeploymentProvenanceTests(unittest.TestCase):
    def test_clean_working_tree_is_accepted(self):
        report = validate_records([service_record(service=s) for s in SERVICES], working_tree_clean=True)
        self.assertTrue(report["passed"])

    def test_dirty_working_tree_is_rejected(self):
        report = validate_records([service_record(service=s) for s in SERVICES], working_tree_clean=False)
        self.assertFalse(report["passed"])
        self.assertIn("working tree is dirty", report["failures"])

    def test_matching_expected_and_image_revision_passes(self):
        self.assertEqual(service_record()["status"], "PASS")

    def test_mismatching_revision_fails(self):
        result = service_record(image_revision="0" * 40)
        self.assertEqual(result["status"], "FAIL")
        self.assertTrue(any("OCI revision" in failure for failure in result["failures"]))

    def test_missing_oci_revision_fails(self):
        result = service_record(image_revision=None)
        self.assertEqual(result["status"], "FAIL")
        self.assertTrue(any("<missing>" in failure for failure in result["failures"]))

    def test_one_failed_service_fails_global_validation(self):
        records = [service_record(service=s) for s in SERVICES]
        records[-1] = service_record(service=SERVICES[-1], image_revision=None)
        report = validate_records(records, working_tree_clean=True)
        self.assertFalse(report["passed"])
        self.assertEqual(len(report["failures"]), 1)

    def test_only_config_digest_is_compared_with_pod_runtime_id(self):
        self.assertEqual(normalize_config_digest(CONFIG_ID), CONFIG_ID)
        self.assertEqual(
            normalize_config_digest("containerd://registry/service@" + CONFIG_ID),
            None,
        )
        self.assertEqual(normalize_config_digest("containerd://" + CONFIG_ID), CONFIG_ID)
        self.assertIsNone(normalize_config_digest("sha256:short"))


if __name__ == "__main__":
    unittest.main()
