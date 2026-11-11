"""Deterministic in-process dependency HTTP transport for unit tests; no TCP services."""
import json
import requests
from opentelemetry import context
from demostore import create_app as storefront
from demostore.dependencies import create_dependency


def create_app(config):
    app = storefront(config)
    dependencies = {}
    def transport(component, path, encoded, headers, timeout):
        payload = json.loads(encoded)
        if component == "bank" and path == "/charge" and payload["scenario"] == "timeout":
            # Unit tests inject the transport deadline. Live acceptance separately
            # requires the real 1-second client timeout and late bank response.
            raise requests.Timeout()
        # A real network receiver has no caller-local active span. Extract only the
        # W3C headers, so unit tests also exercise SERVER rather than nested INTERNAL.
        token = context.attach(context.Context())
        try:
            response = dependencies[component].test_client().post(path, data=encoded, headers=headers)
        finally:
            context.detach(token)
        return response.status_code, response.get_json()
    for component in ("inventory", "payment", "bank"):
        child_config = {key: value for key, value in app.config.items() if key in
                        {"TESTING", "SECRET_KEY", "RELEASE_ID", "TELEMETRY_ENABLED", "TELEMETRY_TEST_EXPORTER"}}
        dependencies[component] = create_dependency(component, {**child_config, "DEPENDENCY_TRANSPORT": transport})
    app.config["DEPENDENCY_TRANSPORT"] = transport
    app.extensions["demostore.test_dependencies"] = dependencies
    return app
