import unittest

from scripts.experiment.deployment.verify_provenance import (
    REVISION_LABEL,
    SERVICES,
    validate_all,
    validate_service,
    working_tree_is_clean,
)


class DeploymentProvenanceTests(unittest.TestCase):
    revision = "a" * 40
    image_id = "sha256:" + "b" * 64
    image_reference = f"tcc-experiment-v2/paymentservice:{revision}"

    def service_record(self, revision=None, include_label=True):
        labels = {REVISION_LABEL: revision or self.revision} if include_label else {}
        return validate_service(
            "paymentservice",
            self.revision,
            self.image_reference,
            {
                "Id": self.image_id,
                "RepoTags": [self.image_reference],
                "Config": {"Labels": labels},
            },
            {
                "metadata": {"name": "paymentservice-123"},
                "spec": {
                    "containers": [
                        {
                            "name": "server",
                            "image": self.image_reference,
                            "imagePullPolicy": "IfNotPresent",
                        }
                    ]
                },
                "status": {
                    "containerStatuses": [
                        {
                            "name": "server",
                            "imageID": self.image_id,
                            "ready": True,
                        }
                    ]
                },
            },
            {
                "status": {"id": self.image_id},
                "info": {
                    "imageSpec": {
                        "config": {"Labels": labels}
                    }
                },
            },
        )

    def test_clean_working_tree_is_accepted(self):
        self.assertTrue(working_tree_is_clean(""))
        self.assertTrue(working_tree_is_clean("\n"))

    def test_dirty_working_tree_is_rejected(self):
        self.assertFalse(working_tree_is_clean(" M src/paymentservice/charge.js\n"))

    def test_matching_image_and_pod_revision_passes(self):
        self.assertEqual(self.service_record()["status"], "PASS")

    def test_mismatching_revision_fails(self):
        self.assertEqual(
            self.service_record(revision="c" * 40)["status"], "FAIL"
        )

    def test_missing_oci_revision_fails(self):
        self.assertEqual(
            self.service_record(include_label=False)["status"], "FAIL"
        )

    def test_one_failed_service_fails_global_validation(self):
        records = [
            {"service": service, "status": "PASS"}
            for service in SERVICES
        ]
        records[0] = {"service": SERVICES[0], "status": "FAIL"}
        self.assertEqual(validate_all(records)["status"], "FAIL")


if __name__ == "__main__":
    unittest.main()
