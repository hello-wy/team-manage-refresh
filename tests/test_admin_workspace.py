"""Verify full-page requests and working-area requests render different layouts."""
import unittest
from unittest.mock import patch

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base, get_db
from app.dependencies.auth import require_admin
from app.main import app


class AdminWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

        async def test_db():
            async with self.sessions() as db:
                yield db

        app.dependency_overrides[get_db] = test_db
        app.dependency_overrides[require_admin] = lambda: {"username": "admin", "is_admin": True}
        self.client = AsyncClient(transport=ASGITransport(app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        app.dependency_overrides.clear()
        await self.engine.dispose()

    async def test_top_level_request_keeps_navigation_and_favicon_outside_content(self):
        response = await self.client.get("/admin/codes?search=example")
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="adminWorkspaceFrame"', response.text)
        self.assertIn('src="/admin/codes?search=example"', response.text)
        self.assertIn('rel="icon"', response.text)
        self.assertIn('Sec-Fetch-Dest', response.headers['vary'])
        self.assertIn('class="navbar"', response.text)
        self.assertNotIn('class="workspace-page codes-workspace-page"', response.text)
        self.assertNotIn('/static/js/codes-list-refresh.js', response.text)
        self.assertNotIn('href="/admin/records"', response.text)
        self.assertNotIn('href="/admin/rotation"', response.text)

    async def test_frame_and_ajax_requests_include_content_without_duplicate_header(self):
        headers = [{"Sec-Fetch-Dest": "iframe"}, {"X-Requested-With": "XMLHttpRequest"}]
        for request_headers in headers:
            response = await self.client.get("/admin/codes", headers=request_headers)
            self.assertEqual(response.status_code, 200)
            self.assertIn('X-Requested-With', response.headers['vary'])
            self.assertIn('class="workspace-page codes-workspace-page"', response.text)
            self.assertIn('/static/js/codes-list-refresh.js', response.text)
            self.assertIn('id="generateCodeModal"', response.text)
            self.assertNotIn('id="adminWorkspaceFrame"', response.text)
            self.assertNotIn('class="navbar"', response.text)
            self.assertNotIn('rel="icon"', response.text)

    async def test_removed_pages_are_unavailable(self):
        for path in ("/admin/records", "/admin/rotation"):
            response = await self.client.get(path)
            self.assertEqual(response.status_code, 404)

    async def test_login_page_still_renders_login_form(self):
        with patch("app.main.AsyncSessionLocal", self.sessions):
            response = await self.client.get("/login")
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="loginForm"', response.text)
        self.assertNotIn('id="adminWorkspaceFrame"', response.text)


if __name__ == "__main__":
    unittest.main()
