import unittest
import time
from unittest.mock import patch

import gevent

from .config import WorkloadConfig
from .control import WorkloadControl
from .runner import RequestMetrics, run_open_loop_phase
from .locustfile import execute_checkout_flow, new_flow_session


def test_config(**overrides):
    env = {
        "TCC_RUN_ID": "unit-test",
        "TCC_TARGET_FLOW_RATE": "2",
        "TCC_MAX_IN_FLIGHT_FLOWS": "8",
        "TCC_ARTIFACT_DIR": "/tmp",
    }
    env.update(overrides)
    return WorkloadConfig.from_env(env)


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code
        self.closed = False

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, statuses):
        self.statuses = iter(statuses)
        self.calls = []
        self.closed = False
        self.cookie_jar = object()

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return FakeResponse(next(self.statuses))

    def close(self):
        self.closed = True


class WorkloadConfigTests(unittest.TestCase):
    def test_parses_open_loop_parameters_and_seed(self):
        config = test_config(
            TCC_WORKLOAD_SEED="173", TCC_TARGET_FLOW_RATE="0.75",
            TCC_MAX_IN_FLIGHT_FLOWS="3", TCC_DURATION="30",
        )
        self.assertEqual(config.seed, 173)
        self.assertEqual(config.target_flow_rate, 0.75)
        self.assertEqual(config.max_in_flight_flows, 3)

    def test_rejects_invalid_product_rate_and_capacity(self):
        with self.assertRaisesRegex(ValueError, "catalog"):
            test_config(TCC_PRODUCT_ID="missing")
        with self.assertRaisesRegex(ValueError, "flows per second"):
            test_config(TCC_TARGET_FLOW_RATE="0")
        with self.assertRaisesRegex(ValueError, "at least 1"):
            test_config(TCC_MAX_IN_FLIGHT_FLOWS="0")
        with self.assertRaisesRegex(ValueError, "not used by the open-loop"):
            test_config(TCC_USERS="2")


class CheckoutFlowTests(unittest.TestCase):
    def test_executes_exact_five_requests_and_closes_its_session(self):
        config = test_config()
        session = FakeSession([200, 200, 302, 200, 200])
        recorded = []
        execute_checkout_flow(config, session_factory=lambda: session,
                              record_request=lambda *args: recorded.append(args))
        self.assertEqual([(method, url.removeprefix(config.frontend_url), kwargs["allow_redirects"])
                          for method, url, kwargs in session.calls], [
            ("GET", "/", False),
            ("GET", "/product/OLJCESPC7Z", False),
            ("POST", "/cart", False),
            ("GET", "/cart", False),
            ("POST", "/cart/checkout", False),
        ])
        self.assertEqual(session.calls[2][2]["data"], {"product_id": "OLJCESPC7Z", "quantity": "1"})
        self.assertEqual(session.calls[4][2]["data"], config.checkout)
        self.assertEqual([r[0] for r in recorded], ["home", "product", "add_cart", "cart", "checkout"])
        self.assertTrue(session.closed)
        self.assertNotIn("/cart/empty", [call[1] for call in session.calls])

    def test_each_flow_requests_a_distinct_session(self):
        config = test_config()
        sessions = []

        def factory():
            session = FakeSession([200, 200, 302, 200, 200])
            sessions.append(session)
            return session

        execute_checkout_flow(config, session_factory=factory)
        execute_checkout_flow(config, session_factory=factory)
        self.assertEqual(len(sessions), 2)
        self.assertIsNot(sessions[0], sessions[1])
        self.assertIsNot(sessions[0].cookie_jar, sessions[1].cookie_jar)

    def test_real_requests_sessions_have_independent_cookie_jars(self):
        first = new_flow_session()
        second = new_flow_session()
        try:
            self.assertIsNot(first, second)
            self.assertIsNot(first.cookies, second.cookies)
        finally:
            first.close()
            second.close()

    def test_unexpected_status_fails_at_current_stage(self):
        session = FakeSession([200, 404])
        with self.assertRaisesRegex(Exception, "expected HTTP 200, got 404"):
            execute_checkout_flow(test_config(), session_factory=lambda: session)
        self.assertEqual(len(session.calls), 2)
        self.assertTrue(session.closed)


class OpenLoopSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.config = test_config(TCC_TARGET_FLOW_RATE="4", TCC_MAX_IN_FLIGHT_FLOWS="8")

    def test_slow_flow_does_not_block_scheduled_arrivals(self):
        control = WorkloadControl("unit-test", self.config.max_in_flight_flows, self.config.target_flow_rate)
        start_times = []

        def slow_flow(_config, record_request=None):
            start_times.append(time.monotonic())
            gevent.sleep(0.7)

        greenlets = set()
        with patch("src.loadgenerator.experiment.runner.execute_checkout_flow", slow_flow):
            run_open_loop_phase(config=self.config, control=control, phase="steady",
                                 duration_seconds=1.0, metrics=RequestMetrics(), greenlets=greenlets)
            gevent.joinall(list(greenlets), timeout=2)
        self.assertEqual(len(start_times), 4)
        self.assertGreater(control.peak_in_flight, 1)
        self.assertLess(start_times[1] - start_times[0], 0.5)
        self.assertEqual(control.snapshot("steady")["completed"], 4)

    def test_capacity_drops_arrivals_without_queueing(self):
        config = test_config(TCC_TARGET_FLOW_RATE="5", TCC_MAX_IN_FLIGHT_FLOWS="1")
        control = WorkloadControl("unit-test", config.max_in_flight_flows, config.target_flow_rate)
        greenlets = set()

        def slow_flow(_config, record_request=None):
            gevent.sleep(0.55)

        with patch("src.loadgenerator.experiment.runner.execute_checkout_flow", slow_flow):
            run_open_loop_phase(config=config, control=control, phase="steady",
                                 duration_seconds=0.8, metrics=RequestMetrics(), greenlets=greenlets)
            gevent.joinall(list(greenlets), timeout=2)
        counts = control.snapshot("steady")
        self.assertEqual(counts["scheduled"], 4)
        self.assertGreater(counts["control_plane_dropped"], 0)
        self.assertEqual(counts["started"] + counts["control_plane_dropped"], counts["scheduled"])
        self.assertEqual(control.dropped_by_reason["steady"]["max_in_flight"], counts["control_plane_dropped"])


if __name__ == "__main__":
    unittest.main()
