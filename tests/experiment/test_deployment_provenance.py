import unittest
import hashlib
from io import BytesIO
import json
import tarfile

from scripts.experiment.deployment.verify_provenance import (
    REVISION_LABEL,
    SERVICES,
    validate_all,
    validate_service,
    pod_image_matches_runtime,
    working_tree_is_clean,
)
from scripts.experiment.deployment.image_config_digest import read_image_archive


class DeploymentProvenanceTests(unittest.TestCase):
    revision = "a" * 40
    image_id = "sha256:" + "b" * 64
    manifest_digest = "sha256:" + "c" * 64
    image_reference = f"tcc-experiment-v2/paymentservice:{revision}"

    def service_record(self, revision=None, include_label=True):
        labels = {REVISION_LABEL: revision or self.revision} if include_label else {}
        return validate_service(
            "paymentservice",
            self.revision,
            self.image_reference,
            {
                "Id": self.manifest_digest,
                "RepoTags": [self.image_reference],
                "Config": {"Labels": labels},
                "ExperimentConfigDigest": self.image_id,
                "ExperimentIndexManifestDigest": self.manifest_digest,
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
                "status": {
                    "id": self.image_id,
                    "repoTags": ["docker.io/" + self.image_reference],
                },
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

    def test_docker_save_config_digest_is_extracted_and_hashed(self):
        config = b'{"architecture":"amd64","config":{"Labels":{}}}'
        config_hash = hashlib.sha256(config).hexdigest()
        manifest = json.dumps([{"Config": f"blobs/sha256/{config_hash}"}]).encode()
        index = json.dumps({"manifests": [{"digest": "sha256:" + "d" * 64}]}).encode()
        archive_bytes = BytesIO()
        with tarfile.open(fileobj=archive_bytes, mode="w") as archive:
            for name, content in (
                (f"blobs/sha256/{config_hash}", config),
                ("manifest.json", manifest),
                ("index.json", index),
            ):
                member = tarfile.TarInfo(name)
                member.size = len(content)
                archive.addfile(member, BytesIO(content))

        identity = read_image_archive(BytesIO(archive_bytes.getvalue()))
        self.assertEqual(identity["config_digest"], "sha256:" + config_hash)
        self.assertEqual(identity["index_manifest_digest"], "sha256:" + "d" * 64)

    def test_pod_image_id_may_use_runtime_registered_repo_digest(self):
        repo_digest = "docker.io/library/import-date@sha256:" + "e" * 64
        self.assertTrue(
            pod_image_matches_runtime(
                repo_digest,
                {"repoDigests": [repo_digest]},
                self.image_id,
            )
        )
        self.assertFalse(
            pod_image_matches_runtime(
                repo_digest,
                {"repoDigests": []},
                self.image_id,
            )
        )


if __name__ == "__main__":
    unittest.main()
