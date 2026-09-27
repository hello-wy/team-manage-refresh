import unittest
import json
from unittest.mock import AsyncMock, patch
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models import AccountPoolEntry, AccountPoolWorkspace, MemberAuthorization, Team, TeamEmailMapping
from app.services.account_pool_listing import build_pool_entry_data
from app.services.encryption import encryption_service
from app.services.account_pool_usage import AccountPoolUsageService, parse_usage


class _Response:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class _Client:
    def __init__(self, response):
        self.response = response
        self.headers = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, _url, headers):
        self.headers = headers
        return self.response


class AccountPoolUsageTests(unittest.IsolatedAsyncioTestCase):
    def test_team_usage_requires_matching_workspace_json(self):
        def encrypted(workspace_id):
            return encryption_service.encrypt_token(json.dumps({"accounts": [{
                "credentials": {"access_token": f"token-{workspace_id}",
                                "chatgpt_account_id": workspace_id}
            }]}))

        entry = AccountPoolEntry(workspace_id="team-a", export_json_encrypted=encrypted("team-a"))
        workspace = AccountPoolWorkspace(workspace_id="team-b",
                                         export_json_encrypted=encrypted("team-b"))
        self.assertIsNone(AccountPoolUsageService._entry_token(entry, "team-b"))
        self.assertEqual(AccountPoolUsageService._entry_token(entry, "team-b", workspace),
                         ("token-team-b", "team-b"))

    def test_parse_usage_supports_aliases_and_derived_remaining(self):
        result = parse_usage({
            "rate_limits": {
                "300min": {"used": 20, "limit": 100, "reset_at": "short-reset"},
                "7d": {"remaining": 0, "limit": 500, "resets_at": "weekly-reset"},
            }
        })
        self.assertEqual(result["5h"]["remaining"], 80)
        self.assertEqual(result["5h"]["reset_at"], "short-reset")
        self.assertEqual(result["1week"]["state"], "exhausted")
        self.assertEqual(result["1week"]["used"], 500)

    def test_parse_real_wham_windows_by_duration(self):
        usage = parse_usage({"rate_limit": {
            "primary_window": {
                "limit_window_seconds": 18000, "used_percent": 25.5,
                "reset_at": 1790400000,
            },
            "secondary_window": {
                "limit_window_seconds": 604800, "used_percent": 100,
                "reset_at": 1791000000,
            },
        }})
        self.assertEqual(usage["5h"]["remaining"], 74.5)
        self.assertEqual(usage["5h"]["reset_at"], "1790400000")
        self.assertEqual(usage["1week"]["state"], "exhausted")
        self.assertEqual(usage["1week"]["reset_at"], "1791000000")

    def test_weekly_only_response_does_not_invent_five_hour_quota(self):
        usage = parse_usage({"rate_limit": {
            "primary_window": {
                "limit_window_seconds": 604800, "used_percent": 0,
                "reset_at": 1791000000,
            },
            "secondary_window": None,
        }})
        self.assertIsNone(usage["5h"])
        self.assertEqual(usage["1week"]["remaining"], 100)

    def test_unrelated_or_empty_windows_are_not_valid_quota(self):
        self.assertIsNone(parse_usage({"rate_limit": {"primary_window": {
            "limit_window_seconds": 2592000, "used_percent": 10,
        }}}))
        self.assertIsNone(parse_usage({"rate_limits": {"7d": {"reset_at": 1791000000}}}))

    async def test_only_401_is_invalid(self):
        invalid = AccountPoolUsageService(
            client_factory=lambda **_: _Client(_Response(401)),
        )
        forbidden = AccountPoolUsageService(
            client_factory=lambda **_: _Client(_Response(403)),
        )
        self.assertEqual((await invalid._check_token(("token", "account")))["status"], "invalid")
        self.assertEqual((await forbidden._check_token(("token", "account")))["status"], "unknown")

    async def test_request_uses_browser_fingerprint_account_and_proxy(self):
        client = _Client(_Response(200, {"rate_limit": {"primary_window": {
            "limit_window_seconds": 604800, "used_percent": 10,
            "reset_at": 1791000000,
        }}}))
        factory = unittest.mock.MagicMock(return_value=client)
        service = AccountPoolUsageService(client_factory=factory)
        result = await service._check_token(
            ("token", "account"), {"all": "http://proxy.example:8080"},
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["1week"]["remaining"], 90)
        self.assertEqual(client.headers["Chatgpt-Account-Id"], "account")
        self.assertEqual(client.headers["Authorization"], "Bearer token")
        self.assertEqual(factory.call_args.kwargs["impersonate"], "chrome110")
        self.assertEqual(factory.call_args.kwargs["proxies"]["all"],
                         "http://proxy.example:8080")

    async def test_check_many_reads_member_authorizations_for_missing_pool_tokens(self):
        service = AccountPoolUsageService()
        service._check_token = AsyncMock(return_value={"status": "unavailable"})
        scalars = unittest.mock.MagicMock()
        scalars.all.return_value = []
        result = unittest.mock.MagicMock()
        result.scalars.return_value = scalars
        db = unittest.mock.MagicMock()
        db.execute = AsyncMock(return_value=result)

        with patch.object(service, "_proxies", new=AsyncMock(return_value=None)):
            values = await service.check_many(
                db,
                [("one@example.com", "space-a"), ("two@example.com", "space-b")],
            )

        self.assertEqual(db.execute.await_count, 3)
        self.assertEqual(len(values), 2)
        self.assertEqual(service._check_token.await_count, 2)


class AccountPoolUsageFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.service = AccountPoolUsageService()

    async def asyncTearDown(self):
        await self.engine.dispose()

    @staticmethod
    def payload(space="team-a", token="member-token"):
        return encryption_service.encrypt_token(json.dumps({"accounts": [{
            "credentials": {"access_token": token, "chatgpt_account_id": space}
        }]}))

    def test_real_authorization_credentials_fall_back_to_scoped_export_json(self):
        for raw in (None, "broken", encryption_service.encrypt_token(json.dumps({
                "access_token": "raw-token", "refresh_token": "refresh", "client_id": "client"}))):
            with self.subTest(raw=raw):
                record = MemberAuthorization(account_id="team-a", credentials_encrypted=raw,
                                             export_json_encrypted=self.payload())
                self.assertEqual(self.service._authorization_token(record, "team-a"),
                                 ("member-token", "team-a"))
                self.assertIsNone(self.service._authorization_token(record, "team-b"))
                record.export_json_encrypted = self.payload("team-b")
                self.assertIsNone(self.service._authorization_token(record, "team-a"))

    async def test_empty_or_broken_workspace_json_uses_matching_account_json(self):
        async with self.sessions() as db:
            entry = AccountPoolEntry(email="member@example.com", workspace_id="team-a",
                                     export_json_encrypted=self.payload())
            db.add(entry)
            await db.flush()
            workspace = AccountPoolWorkspace(account_pool_id=entry.id, workspace_id="team-a",
                                              status="workspace_ok")
            db.add(workspace)
            await db.commit()
            for json_value in (None, "broken-encryption", self.payload("other-team")):
                workspace.export_json_encrypted = json_value
                await db.commit()
                tokens = await self.service._tokens_for_accounts(db, [("key", entry.email, "team-a")])
                self.assertEqual(tokens, [("key", ("member-token", "team-a"))])

    async def test_batch_and_single_lookup_use_owner_token_without_leaking_to_other_members(self):
        async with self.sessions() as db:
            db.add(Team(email="owner@example.com", account_id="team-a",
                        access_token_encrypted=encryption_service.encrypt_token("owner-token")))
            await db.commit()
            self.service._check_token = AsyncMock(side_effect=lambda token, proxies:
                {"status": "ok" if token else "unavailable"})
            keys = [("owner@example.com", "team-a"), ("other@example.com", "team-a"),
                    ("owner@example.com", "team-b")]
            with patch.object(self.service, "_proxies", new=AsyncMock(return_value=None)):
                batch = await self.service.check_many(db, keys)
                single = await self.service.check_email(db, *keys[0])
            self.assertEqual(batch[keys[0]], single)
            self.assertEqual(single["status"], "ok")
            self.assertEqual(batch[keys[1]]["status"], "unavailable")
            self.assertEqual(batch[keys[2]]["status"], "unavailable")
            self.service._check_token.assert_any_await(("owner-token", "team-a"), None)

    async def test_joined_members_without_workspace_scan_use_their_team_authorization(self):
        async with self.sessions() as db:
            entry = AccountPoolEntry(email="member@example.com")
            team = Team(email="owner@example.com", account_id="team-a", team_name="Team A",
                        access_token_encrypted="unused")
            db.add_all([entry, team])
            await db.flush()
            db.add_all([
                TeamEmailMapping(team_id=team.id, email=entry.email, status="joined", seat_type="premium"),
                MemberAuthorization(team_id=team.id, email=entry.email, account_id=team.account_id,
                                    credentials_encrypted=encryption_service.encrypt_token(json.dumps({
                                        "access_token": "member-token", "chatgpt_account_id": "team-a",
                                    }))),
            ])
            await db.commit()
            row = (await build_pool_entry_data(db, [entry]))[0]
            self.assertIsNone(row["workspace_id"])
            self.assertEqual(row["quota_workspace_id"], "team-a")
            self.assertEqual(row["quota_team_id"], team.id)
            normalized = self.service._normalize_accounts([(row["email"], row["quota_workspace_id"])])
            tokens = await self.service._tokens_for_accounts(db, normalized)
            self.assertEqual(tokens[0][1], ("member-token", "team-a"))
            from app.routes import admin
            self.service._check_token = AsyncMock(return_value={
                "status": "ok", "error": None,
                "1week": {"used": 25, "remaining": 75, "limit": 100, "reset_at": None},
            })
            with patch.object(admin, "account_pool_usage_service", self.service), patch.object(
                self.service, "_proxies", new=AsyncMock(return_value=None)
            ):
                attached = await admin._attach_account_pool_usage(db, [row])
            self.assertEqual(attached[0]["usage"]["1week"]["remaining"], 75)
            self.assertTrue(attached[0]["team_usage"]["premium_used"])
            self.service._check_token.assert_awaited_once_with(("member-token", "team-a"), None)
            # 页面显示当前成员的 Team 额度；个人空间仍保留为用户的导出选择。
            entry.workspace_id = "personal"
            entry.workspace_status = "personal_account"
            await db.commit()
            row = (await build_pool_entry_data(db, [entry]))[0]
            self.assertEqual(row["workspace_id"], "personal")
            self.assertEqual(row["display_team_name"], "Team A")
            self.assertEqual(row["quota_workspace_id"], "team-a")
            self.assertEqual(row["quota_team_id"], team.id)
            self.assertTrue(row["workspace_is_personal"])
            self.assertEqual(row["team_usage"]["seat_type"], "premium")
            self.assertEqual((await self.service._tokens_for_accounts(
                db, self.service._normalize_accounts([(row["email"], row["quota_workspace_id"])]),
            ))[0][1], ("member-token", "team-a"))

    async def test_team_column_only_displays_team_and_seat(self):
        from starlette.requests import Request
        from app.routes import admin
        from app.services.account_pool_team_usage import record_seat_switch
        async with self.sessions() as db:
            entry = AccountPoolEntry(email="member@example.com")
            team = Team(email="owner@example.com", account_id="team-a", access_token_encrypted="unused")
            db.add_all([entry, team])
            await db.flush()
            mapping = TeamEmailMapping(team_id=team.id, email=entry.email, status="joined", seat_type="standard")
            db.add(mapping)
            await record_seat_switch(db, team_id=team.id, email=entry.email, seat_type="standard")
            await db.commit()
            request = Request({"type": "http", "method": "GET", "path": "/admin/account-pool",
                               "headers": [], "query_string": b"", "server": ("test", 80)})
            with patch.object(admin, "_attach_account_pool_usage", new=AsyncMock(side_effect=lambda db, rows: rows)):
                response = await admin.account_pool_page(request, 1, 20, "member@", "", db, {"username": "admin"})
                self.assertNotIn("高级待确认", response.body.decode())
                self.assertNotIn("高级使用情况待确认", response.body.decode())
                mapping.seat_type = "premium"
                await record_seat_switch(db, team_id=team.id, email=entry.email, seat_type="premium")
                await db.commit()
                response = await admin.account_pool_page(request, 1, 20, "member@", "", db, {"username": "admin"})
                self.assertNotIn("高级使用情况待确认", response.body.decode())
                self.assertNotIn("account-pool-premium-note", response.body.decode())
                self.assertNotIn("account-pool-status status-badge", response.body.decode())
                response = await admin.account_pool_page(request, 1, 20, "owner@", "", db, {"username": "admin"})
                self.assertNotIn("account-pool-owner-badge", response.body.decode())
                self.assertIn("所有者保留", response.body.decode())

                from app.models import TeamReplacementQueue
                from app.utils.time_utils import get_now
                db.add_all([AccountPoolEntry(email="candidate@example.com"),
                            TeamReplacementQueue(team_id=team.id, seat_type="standard",
                                                 last_error_code="seat_limit_exceeded", last_attempt_at=get_now())])
                await db.commit()
                response = await admin.account_pool_page(request, 1, 20, "candidate@", "", db, {"username": "admin"})
                self.assertIn("等待标准席位空位", response.body.decode())
                self.assertNotIn('data-replacement-at=""', response.body.decode())
                self.assertNotIn("等待执行", response.body.decode())

    async def test_multiple_teams_without_selected_workspace_are_not_guessed(self):
        async with self.sessions() as db:
            entry = AccountPoolEntry(email="member@example.com")
            db.add(entry)
            for space in ("team-a", "team-b"):
                team = Team(email=f"{space}@example.com", account_id=space, access_token_encrypted="unused")
                db.add(team)
                await db.flush()
                db.add(TeamEmailMapping(team_id=team.id, email=entry.email, status="joined"))
            await db.commit()
            row = (await build_pool_entry_data(db, [entry]))[0]
            self.assertEqual(row["status"], "conflict")
            self.assertIsNone(row["workspace_id"])
            self.assertFalse(row["quota_workspace_id"])


if __name__ == "__main__":
    unittest.main()
