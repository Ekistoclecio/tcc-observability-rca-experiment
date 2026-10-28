import unittest

from scripts.experiment.orchestrator.preflight import chaos_mesh_combo_supported


class ChaosMeshCompatibilityTests(unittest.TestCase):
    def test_kubernetes_135_and_chaos_mesh_286_are_supported(self):
        self.assertTrue(chaos_mesh_combo_supported("v1.35.8", "v2.8.6"))

    def test_rejects_unsupported_kubernetes_or_chaos_mesh_versions(self):
        self.assertFalse(chaos_mesh_combo_supported("v1.37.0", "v2.8.6"))
        self.assertFalse(chaos_mesh_combo_supported("v1.35.8", "v2.7.3"))


if __name__ == "__main__":
    unittest.main()
