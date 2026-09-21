import unittest
from datetime import timedelta
from unittest.mock import AsyncMock

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models import (
    AccountPoolEntry, AccountPoolHistory, MemberAuthorization, Team, TeamEmailMapping,
)
from app.services.account_pool import AccountPoolService
from app.services.account_pool_credentials import AccountPoolCredentialService
from app.services.encryption import encryption_service
from app.services.team import TeamService
from app.utils.time_utils import get_now


class AccountPoolServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.service = AccountPoolService(
            AccountPoolCredentialService(encryption_service)
        )

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_batch_add_normalizes_and_deduplicates_emails(self):
        async with self.sessions() as session:
            result = await self.service.add_emails(
                session,
                content=" Alice@Example.com\nalice@example.com\ninvalid\n",
            )
            self.assertEqual(result["added"], ["alice@example.com"])
            self.assertEqual(result["invalid"], ["invalid"])

            repeat = await self.service.add_emails(session, emails=["ALICE@example.com"])
            self.assertTrue(repeat["success"])
            self.assertEqual(repeat["existing"], ["alice@example.com"])

    async def test_existing_email_is_not_assigned_a_seat_type(self):
        async with self.sessions() as session:
            await self.service.add_emails(session, emails=["member@example.com"])
            result = await self.service.add_emails(session, emails=["member@example.com"])
            listing = await self.service.list_entries(session)

            self.assertEqual(result["existing"], ["member@example.com"])
            self.assertIsNone(listing["entries"][0]["seat_type"])

    async def test_batch_add_accepts_email_password_and_two_factor_secret(self):
        async with self.sessions() as session:
            result = await self.service.add_emails(
                session,
                content=(
                    "NancyCarterH918@gmail.com----OCRvy*pCSdL3RPZi----"
                    "RV3B7W3PZWC2IPIHPJ2TT6SAATDDCQJ2\n"
                    "plain@example.com"
                ),
            )
            credential_service = AccountPoolCredentialService(encryption_service)
            credentials = await credential_service.get_credentials(
                session, "nancycarterh918@gmail.com"
            )

            self.assertEqual(
                result["added"],
                ["nancycarterh918@gmail.com", "plain@example.com"],
            )
            self.assertEqual(credentials["password"], "OCRvy*pCSdL3RPZi")
            self.assertEqual(
                credentials["two_factor_secret"],
                "RV3B7W3PZWC2IPIHPJ2TT6SAATDDCQJ2",
            )

    async def test_deleted_account_is_hidden_and_not_selected_as_replacement(self):
        async with self.sessions() as session:
            await self.service.add_emails(
                session,
                emails=["deleted@example.com", "available@example.com"],
            )
            deleted_entry = (
                await session.execute(
                    select(AccountPoolEntry).where(
                        AccountPoolEntry.email == "deleted@example.com"
                    )
                )
            ).scalar_one()
            credential_service = AccountPoolCredentialService(encryption_service)
            await credential_service.delete_entry(session, deleted_entry.id)

            listing = await self.service.list_entries(session)
            candidate = await self.service.find_replacement_candidate(1, session)

            self.assertEqual(
                [entry["email"] for entry in listing["entries"]],
                ["available@example.com"],
            )
            self.assertEqual(candidate.email, "available@example.com")

    async def test_invited_replacement_persists_export_queue(self):
        async with self.sessions() as session:
            session.add(Team(
                id=1, email="owner@example.com", account_id="account-1",
                access_token_encrypted="token", status="active",
            ))
            await session.commit()
            await self.service.add_emails(session, emails=["member@example.com"])

            result = await self.service.invite_replacement(
                1, session,
                invite_member=AsyncMock(return_value={"success": True, "status": "invited"}),
            )
            mapping = (await session.execute(select(TeamEmailMapping))).scalar_one()

            self.assertEqual(result["email"], "member@example.com")
            self.assertTrue(mapping.replacement_export_pending)
            self.assertEqual(mapping.status, "invited")

    async def test_failed_replacement_preserves_upstream_error_details(self):
        async with self.sessions() as session:
            session.add(Team(
                id=1, email="owner@example.com", account_id="account-1",
                access_token_encrypted="token", status="active",
            ))
            await session.commit()
            await self.service.add_emails(session, emails=["member@example.com"])

            result = await self.service.invite_replacement(
                1,
                session,
                invite_member=AsyncMock(return_value={
                    "success": False,
                    "error": "请求冲突",
                    "error_code": "seat_operation_pending",
                    "status_code": 409,
                }),
            )

            self.assertEqual(result["email"], "member@example.com")
            self.assertEqual(result["error_code"], "seat_operation_pending")
            self.assertEqual(result["status_code"], 409)

    async def test_reinvite_invalidates_old_authorization_and_export_state(self):
        async with self.sessions() as session:
            session.add(Team(
                id=1, email="owner@example.com", account_id="account-1",
                access_token_encrypted="token", status="active",
            ))
            session.add(MemberAuthorization(
                team_id=1, email="member@example.com", account_id="account-1",
                credentials_encrypted="old", export_json_encrypted="old-json",
                sub2api_account_id=42, sub2api_exported_at=get_now(),
            ))
            await session.commit()
            await self.service.add_emails(session, emails=["member@example.com"])

            await self.service.invite_replacement(
                1, session,
                invite_member=AsyncMock(return_value={"success": True, "status": "invited"}),
            )
            record = (await session.execute(select(MemberAuthorization))).scalar_one()

            self.assertIsNone(record.credentials_encrypted)
            self.assertIsNone(record.export_json_encrypted)
            self.assertIsNone(record.sub2api_exported_at)

    async def test_join_and_leave_creates_reopenable_history(self):
        async with self.sessions() as session:
            team = Team(
                id=1,
                email="owner@example.com",
                team_name="Alpha",
                account_id="account-1",
                access_token_encrypted="token",
                status="active",
                max_members=6,
                current_members=1,
            )
            session.add(team)
            await session.commit()
            await self.service.add_emails(session, emails=["member@example.com"])

            joined_at = get_now() - timedelta(hours=1)
            team_service = TeamService.__new__(TeamService)
            members = {
                "member@example.com": {
                    "id": "user-1",
                    "seat_type": "prolite",
                    "created_time": joined_at.isoformat(),
                }
            }
            await team_service._reconcile_team_email_mappings(
                1, set(members), set(), session, joined_members=members
            )
            await session.commit()
            histories = (await session.execute(select(AccountPoolHistory))).scalars().all()
            self.assertEqual(len(histories), 1)
            self.assertIsNone(histories[0].left_at)

            mapping = (await session.execute(select(TeamEmailMapping))).scalar_one()
            self.assertEqual(mapping.seat_type, "premium")
            listing = await self.service.list_entries(session)
            self.assertEqual(listing["entries"][0]["seat_type"], "premium")
            mapping.missing_sync_count = 3
            await team_service._reconcile_team_email_mappings(1, set(), set(), session)
            await session.commit()
            history = (await session.execute(select(AccountPoolHistory))).scalar_one()
            self.assertIsNotNone(history.left_at)
            self.assertIsNone(mapping.seat_type)

    async def test_current_membership_has_one_team_priority(self):
        async with self.sessions() as session:
            teams = [
                Team(id=1, email="owner-1@example.com", team_name="Alpha", status="active", max_members=6, access_token_encrypted="token-1"),
                Team(id=2, email="owner-2@example.com", team_name="Beta", status="active", max_members=6, access_token_encrypted="token-2"),
            ]
            session.add_all(teams)
            await session.commit()
            await self.service.add_emails(session, emails=["member@example.com"])
            session.add_all([
                TeamEmailMapping(
                    team_id=1,
                    email="member@example.com",
                    status="joined",
                    seat_type="standard",
                ),
                TeamEmailMapping(team_id=2, email="member@example.com", status="invited"),
            ])
            await session.commit()
            listing = await self.service.list_entries(session)
            self.assertEqual(listing["entries"][0]["status"], "conflict")
            self.assertEqual(listing["entries"][0]["team_id"], 1)
            self.assertEqual(listing["entries"][0]["seat_type"], "standard")

    async def test_replacement_skips_active_and_recently_invited_accounts(self):
        async with self.sessions() as session:
            session.add_all([
                Team(
                    id=1,
                    email="owner-1@example.com",
                    status="active",
                    max_members=6,
                    access_token_encrypted="token-1",
                ),
                Team(
                    id=2,
                    email="owner-2@example.com",
                    status="active",
                    max_members=6,
                    access_token_encrypted="token-2",
                ),
            ])
            await session.commit()
            await self.service.add_emails(
                session,
                emails=["active@example.com", "recent@example.com"],
            )
            await self.service.add_emails(session, emails=["eligible@example.com"])
            now = get_now()
            session.add_all([
                TeamEmailMapping(
                    team_id=2,
                    email="active@example.com",
                    status="joined",
                ),
                TeamEmailMapping(
                    team_id=1,
                    email="recent@example.com",
                    status="removed",
                    last_invited_at=now - timedelta(days=1),
                ),
                TeamEmailMapping(
                    team_id=1,
                    email="eligible@example.com",
                    status="removed",
                    last_invited_at=now - timedelta(days=8),
                ),
            ])
            await session.commit()
            invite_member = AsyncMock(
                return_value={"success": True, "status": "invited"}
            )

            result = await self.service.invite_replacement(
                1,
                session,
                invite_member=invite_member,
            )

            self.assertEqual(result["email"], "eligible@example.com")
            invite_member.assert_awaited_once_with(
                1,
                "eligible@example.com",
                session,
                seat_type="standard",
            )

    async def test_invite_options_only_include_currently_available_accounts(self):
        async with self.sessions() as session:
            session.add_all([
                Team(id=1, email="owner@example.com", status="active", max_members=6, access_token_encrypted="token-1"),
                Team(id=2, email="other-owner@example.com", status="active", max_members=6, access_token_encrypted="token-2"),
            ])
            await session.commit()
            await self.service.add_emails(
                session,
                emails=[
                    "owner@example.com",
                    "active@example.com",
                    "recent@example.com",
                    "old@example.com",
                ],
            )
            await self.service.add_emails(session, emails=["available@example.com"])
            entries = {
                entry.email: entry
                for entry in (await session.execute(select(AccountPoolEntry))).scalars().all()
            }
            session.add_all([
                TeamEmailMapping(team_id=2, email="active@example.com", status="joined"),
                AccountPoolHistory(
                    account_pool_id=entries["recent@example.com"].id,
                    team_id=1,
                    team_name="Alpha",
                    team_email="owner@example.com",
                    joined_at=get_now() - timedelta(days=1),
                    left_at=get_now(),
                ),
                AccountPoolHistory(
                    account_pool_id=entries["old@example.com"].id,
                    team_id=1,
                    team_name="Alpha",
                    team_email="owner@example.com",
                    joined_at=get_now() - timedelta(days=8),
                    left_at=get_now() - timedelta(days=7),
                ),
            ])
            await session.commit()

            options = await self.service.list_invite_options(1, session)

            self.assertEqual(
                [option["email"] for option in options],
                ["available@example.com", "old@example.com", "recent@example.com"],
            )
            self.assertEqual(
                {
                    option["email"]: option["recently_joined"]
                    for option in options
                },
                {
                    "available@example.com": False,
                    "old@example.com": False,
                    "recent@example.com": True,
                },
            )

    async def test_invite_options_return_none_for_unknown_team(self):
        async with self.sessions() as session:
            self.assertIsNone(await self.service.list_invite_options(999, session))

    async def test_team_reinvite_cooldown_survives_member_removal(self):
        async with self.sessions() as session:
            team = Team(
                id=1,
                email="owner@example.com",
                status="active",
                max_members=6,
                access_token_encrypted="token",
            )
            session.add(team)
            await session.commit()
            invited_at = get_now() - timedelta(days=1)
            team_service = TeamService.__new__(TeamService)
            await team_service.upsert_team_email_mapping(
                team.id,
                "member@example.com",
                "invited",
                session,
                invited_at=invited_at,
            )
            await team_service.mark_team_email_mapping_removed(
                team.id,
                "member@example.com",
                session,
            )
            await session.commit()

            retry_at = await team_service.get_reinvite_retry_at(
                team.id,
                "member@example.com",
                session,
            )

            self.assertEqual(retry_at, invited_at + timedelta(days=7))


class AccountPoolInputTests(unittest.TestCase):
    def test_parse_emails_preserves_input_order(self):
        normalized, invalid = AccountPoolService.parse_emails(
            content="B@example.com\na@example.com\nB@example.com\n"
        )
        self.assertEqual(normalized, ["b@example.com", "a@example.com"])
        self.assertEqual(invalid, [])


if __name__ == "__main__":
    unittest.main()
