"""Locust user executing one fixed Online Boutique checkout flow at a time."""

from locust import FastHttpUser, task


class CheckoutFlowUser(FastHttpUser):
    abstract = False

    def wait_time(self):
        return self.environment.tcc_control.config.wait_seconds

    def on_start(self):
        self.tcc_control = self.environment.tcc_control
        self.tcc_config = self.tcc_control.config
        self.tcc_user_id = self.tcc_control.allocate_user_id()

    def _request(self, stage, method, path, expected_status, **kwargs):
        kwargs["name"] = stage
        kwargs["catch_response"] = True
        kwargs["allow_redirects"] = False
        with getattr(self.client, method)(path, **kwargs) as response:
            if response.status_code != expected_status:
                response.failure(f"expected HTTP {expected_status}, got {response.status_code}")
                raise FlowFailure(stage, response.status_code, f"unexpected HTTP {response.status_code}")
            response.success()
            return response

    @task
    def checkout_flow(self):
        flow = self.tcc_control.begin_flow(self.tcc_user_id)
        if flow is None:
            return

        stage = "cart_reset"
        try:
            # Each Locust user owns a separate cookie jar/session. Reset before
            # every iteration so a failed prior flow cannot leave cart state.
            self._request(stage, "post", "/cart/empty", 302)

            stage = "home"
            self._request(stage, "get", "/", 200)

            stage = "product"
            self._request(stage, "get", f"/product/{self.tcc_config.product_id}", 200)

            stage = "add_cart"
            self._request(stage, "post", "/cart", 302, data={
                "product_id": self.tcc_config.product_id,
                "quantity": "1",
            })

            stage = "cart"
            self._request(stage, "get", "/cart", 200)

            stage = "checkout"
            self._request(stage, "post", "/cart/checkout", 200, data=self.tcc_config.checkout)
            self.tcc_control.end_flow(flow, completed=True)
        except FlowFailure as exc:
            self.tcc_control.end_flow(flow, completed=False, failure={
                "stage": exc.stage,
                "http_status": exc.http_status,
                "error": str(exc),
            })
        except Exception as exc:  # network errors are classified at the active stage
            self.tcc_control.end_flow(flow, completed=False, failure={
                "stage": stage,
                "error": f"{type(exc).__name__}: {exc}",
            })


class FlowFailure(Exception):
    def __init__(self, stage, http_status, message):
        super().__init__(message)
        self.stage = stage
        self.http_status = http_status
