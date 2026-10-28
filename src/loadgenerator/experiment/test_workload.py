import unittest
from types import SimpleNamespace

from .config import WorkloadConfig
from .control import WorkloadControl
from .locustfile import CheckoutFlowUser


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code
        self.failure_reason = None
        self.was_successful = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def failure(self, reason):
        self.failure_reason = reason

    def success(self):
        self.was_successful = True


class FakeClient:
    def __init__(self, statuses):
        self.statuses = iter(statuses)
        self.calls = []

    def _request(self, method, path, **kwargs):
        self.calls.append((method.upper(), path, kwargs))
        return FakeResponse(next(self.statuses))

    def get(self, path, **kwargs):
        return self._request("get", path, **kwargs)

    def post(self, path, **kwargs):
        return self._request("post", path, **kwargs)


def make_user(statuses):
    config = WorkloadConfig.from_env({
        "TCC_RUN_ID": "unit-test",
        "TCC_ARTIFACT_DIR": "/tmp",
    })
    control = WorkloadControl(config.run_id)
    control.config = config
    control.accepting_flows = True
    control.measurement_enabled = True
    user = object.__new__(CheckoutFlowUser)
    user.environment = SimpleNamespace(tcc_control=control)
    user.tcc_control = control
    user.tcc_config = config
    user.tcc_user_id = "user-0001"
    user.client = FakeClient(statuses)
    user.cart_needs_reset = False
    return user, control


class WorkloadConfigTests(unittest.TestCase):
    def test_parses_seed_and_validation_parameters(self):
        config = WorkloadConfig.from_env({
            "TCC_RUN_ID": "validation-001",
            "TCC_WORKLOAD_SEED": "173",
            "TCC_USERS": "3",
            "TCC_SPAWN_RATE": "0.5",
            "TCC_DURATION": "30",
            "TCC_WARMUP_DURATION": "5",
            "TCC_COOLDOWN_DURATION": "4",
            "TCC_PRODUCT_ID": "OLJCESPC7Z",
        })
        self.assertEqual(config.seed, 173)
        self.assertEqual(config.users, 3)
        self.assertEqual(config.spawn_rate, 0.5)
        self.assertEqual(config.product_id, "OLJCESPC7Z")

    def test_rejects_unknown_product_and_nonpositive_rate(self):
        with self.assertRaisesRegex(ValueError, "catalog"):
            WorkloadConfig.from_env({"TCC_PRODUCT_ID": "missing"})
        with self.assertRaisesRegex(ValueError, "users per second"):
            WorkloadConfig.from_env({"TCC_SPAWN_RATE": "0"})


class WorkloadControlTests(unittest.TestCase):
    def test_ids_and_flow_accounting_are_deterministic(self):
        control = WorkloadControl("validation-001")
        self.assertEqual(control.allocate_user_id(), "user-0001")
        self.assertEqual(control.allocate_user_id(), "user-0002")
        control.accepting_flows = True
        control.measurement_enabled = True
        flow = control.begin_flow("user-0001")
        self.assertEqual(flow["flow_id"], "validation-001-flow-000001")
        control.end_flow(flow, completed=True)
        self.assertEqual((control.flows_started, control.flows_completed), (1, 1))

    def test_failed_flow_is_counted_at_its_stage(self):
        control = WorkloadControl("validation-001")
        control.accepting_flows = True
        control.measurement_enabled = True
        flow = control.begin_flow("user-0001")
        control.end_flow(flow, completed=False, failure={
            "stage": "checkout", "http_status": 500, "error": "HTTP 500"
        })
        self.assertEqual(control.flows_failed, 1)
        self.assertEqual(control.failures_by_stage["checkout"], 1)
        self.assertEqual(control.failed_flow_samples[0]["http_status"], 500)


class CheckoutFlowTests(unittest.TestCase):
    def test_executes_fixed_sequence_with_controlled_test_data(self):
        user, control = make_user([302, 200, 200, 302, 200, 200])
        user.checkout_flow()
        self.assertEqual(
            [(method, path) for method, path, _ in user.client.calls],
            [
                ("POST", "/cart/empty"),
                ("GET", "/"),
                ("GET", "/product/OLJCESPC7Z"),
                ("POST", "/cart"),
                ("GET", "/cart"),
                ("POST", "/cart/checkout"),
            ],
        )
        self.assertEqual(user.client.calls[3][2]["data"], {
            "product_id": "OLJCESPC7Z", "quantity": "1"
        })
        self.assertEqual(user.client.calls[5][2]["data"], user.tcc_config.checkout)
        self.assertEqual(control.flows_completed, 1)
        self.assertEqual(control.flows_failed, 0)

    def test_records_unexpected_status_by_flow_stage(self):
        user, control = make_user([302, 200, 200, 302, 200, 500])
        user.checkout_flow()
        self.assertEqual(control.flows_failed, 1)
        self.assertEqual(control.failed_flow_samples[0]["stage"], "checkout")
        self.assertEqual(control.failed_flow_samples[0]["http_status"], 500)


if __name__ == "__main__":
    unittest.main()
