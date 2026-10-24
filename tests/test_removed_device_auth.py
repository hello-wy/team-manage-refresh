"""Retired device-auth endpoints cannot change upstream workspace settings."""
import unittest

from fastapi.testclient import TestClient

from app.dependencies.auth import require_admin
from app.main import app


class RemovedDeviceAuthTests(unittest.TestCase):
    def test_old_single_batch_and_warranty_endpoints_are_unavailable_to_admins(self):
        previous = app.dependency_overrides.copy()
        app.dependency_overrides[require_admin] = lambda: {"is_admin": True}
        try:
            client = TestClient(app)
            for path, body in (
                ("/admin/teams/1/enable-device-auth", {}),
                ("/admin/teams/batch-enable-device-auth", {"ids": [1]}),
                ("/warranty/enable-device-auth", {"team_id": 1}),
            ):
                with self.subTest(path=path):
                    response = client.post(path, json=body)
                    self.assertIn(response.status_code, (404, 405))
        finally:
            app.dependency_overrides.clear()
            app.dependency_overrides.update(previous)

    def test_api_schema_no_longer_advertises_device_auth(self):
        schema = app.openapi()
        self.assertFalse(any("device-auth" in path for path in schema["paths"]))
        record = schema["components"]["schemas"]["WarrantyCheckRecord"]["properties"]
        self.assertNotIn("device_code_auth_enabled", record)
