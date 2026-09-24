import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import MemberAuthorization, Team, TeamEmailMapping
from app.services.member_auto_kick import MemberAutoKickService
from app.services.replacement_export import ReplacementExportService
from app.services.sub2api import Sub2apiImportUncertain

EMAIL = "member@example.com"


class ReplacementExportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.authorization = SimpleNamespace(
            check=AsyncMock(), automatic_login=AsyncMock(),
            export=AsyncMock(return_value={"accounts": [{"name": "member"}]}),
            mark_sub2api_exported=AsyncMock(),
        )
        self.sub2api = SimpleNamespace(import_member=AsyncMock(
            return_value={"account_id": 42, "group_count": 1}
        ))
        self.exporter = ReplacementExportService(self.authorization, self.sub2api)
        self.runner = MemberAutoKickService()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _pending(self, session):
        session.add(Team(
            id=1, email="owner@example.com", account_id="workspace",
            access_token_encrypted="token",
        ))
        session.add(TeamEmailMapping(
            team_id=1, email=EMAIL, status="invited",
            replacement_export_pending=True,
        ))
        session.add(MemberAuthorization(team_id=1, account_id="workspace", email=EMAIL))
        await session.commit()
        return (await session.execute(select(TeamEmailMapping))).scalar_one()

    async def test_invite_logins_then_waits_until_joined_and_exports_json(self):
        async with self.sessions() as session:
            mapping = await self._pending(session)
            self.authorization.check.side_effect = [
                {"authorized": False, "sub2api_exported": False},
                {"authorized": True, "sub2api_exported": False,
                 "membership": "joined"},
            ]
            self.authorization.automatic_login.return_value = {
                "authorized": True, "membership": "invited"
            }

            waiting = await self.runner.process_pending_exports(
                session, self.exporter.complete
            )
            self.assertEqual(waiting["waiting"], 1)
            self.assertTrue(mapping.replacement_export_pending)
            self.authorization.export.assert_not_awaited()
            self.sub2api.import_member.assert_not_awaited()

            exported = await self.runner.process_pending_exports(
                session, self.exporter.complete
            )
            self.assertEqual(exported["exported"], 1)
            self.assertFalse(mapping.replacement_export_pending)
            self.authorization.automatic_login.assert_awaited_once()
            self.sub2api.import_member.assert_awaited_once_with(
                self.authorization.export.return_value, session
            )
            self.authorization.mark_sub2api_exported.assert_awaited_once_with(
                1, EMAIL, 42, session
            )

    async def test_import_failure_keeps_pending_and_retries_without_login(self):
        async with self.sessions() as session:
            mapping = await self._pending(session)
            mapping_id = mapping.id
            self.authorization.check.return_value = {
                "authorized": True, "sub2api_exported": False,
                "membership": "joined",
            }
            self.sub2api.import_member.side_effect = [
                ValueError("sub2api 暂不可用"),
                {"account_id": 42, "group_count": 1},
            ]

            with self.assertLogs("app.services.member_auto_kick", level="ERROR"):
                failed = await self.runner.process_pending_exports(
                    session, self.exporter.complete
                )
            self.assertFalse(failed["success"])
            self.assertEqual(failed["failed"], 1)
            self.assertTrue((await session.get(
                TeamEmailMapping, mapping_id
            )).replacement_export_pending)

            retried = await self.runner.process_pending_exports(
                session, self.exporter.complete
            )
            self.assertEqual(retried["exported"], 1)
            self.assertFalse(mapping.replacement_export_pending)
            self.authorization.automatic_login.assert_not_awaited()
            self.assertEqual(self.sub2api.import_member.await_count, 2)

    async def test_existing_export_is_not_created_again(self):
        async with self.sessions() as session:
            mapping = await self._pending(session)
            self.authorization.check.return_value = {"sub2api_exported": True}

            stats = await self.runner.process_pending_exports(
                session, self.exporter.complete
            )

            self.assertEqual(stats["exported"], 1)
            self.assertFalse(mapping.replacement_export_pending)
            self.authorization.automatic_login.assert_not_awaited()
            self.sub2api.import_member.assert_not_awaited()

    async def test_uncertain_import_is_recorded_and_not_retried(self):
        async with self.sessions() as session:
            await self._pending(session)
            self.authorization.check.return_value = {
                "authorized": True, "sub2api_exported": False, "membership": "joined",
            }
            self.sub2api.import_member.side_effect = Sub2apiImportUncertain("结果不确定")
            with self.assertLogs("app.services.member_auto_kick", level="ERROR"):
                await self.runner.process_pending_exports(session, self.exporter.complete)
            with self.assertLogs("app.services.member_auto_kick", level="ERROR"):
                await self.runner.process_pending_exports(session, self.exporter.complete)
            self.assertEqual(self.sub2api.import_member.await_count, 1)
            record = (await session.execute(select(MemberAuthorization))).scalar_one()
            self.assertTrue(record.sub2api_import_uncertain)

    async def test_failed_member_does_not_block_the_next_pending_member(self):
        async with self.sessions() as session:
            first = await self._pending(session)
            session.add(TeamEmailMapping(
                team_id=1, email="next@example.com", status="joined",
                replacement_export_pending=True,
            ))
            await session.commit()
            complete = AsyncMock(side_effect=[ValueError("登录失败"), "exported"])

            with self.assertLogs("app.services.member_auto_kick", level="ERROR"):
                stats = await self.runner.process_pending_exports(session, complete)
            mappings = (await session.execute(
                select(TeamEmailMapping).order_by(TeamEmailMapping.id)
            )).scalars().all()

            self.assertEqual((stats["failed"], stats["exported"]), (1, 1))
            self.assertTrue(mappings[0].replacement_export_pending)
            self.assertFalse(mappings[1].replacement_export_pending)
            self.assertEqual(complete.await_count, 2)


if __name__ == "__main__":
    unittest.main()
