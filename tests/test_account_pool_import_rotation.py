import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import BackgroundTasks
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models import AccountPoolEntry, AccountPoolTotpJob
from app.routes.admin import AccountPoolAddRequest, add_account_pool_emails
from app.services.account_pool_batch import AccountPoolBatchService
from app.services.encryption import encryption_service
from app.utils.time_utils import get_now

SECRET = 'JBSWY3DPEHPK3PXP'


def record(email):
    return f'{email}----example-password----{SECRET}'


class AccountPoolImportRotationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine('sqlite+aiosqlite:///:memory:')
        self.sessions = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.totp = SimpleNamespace(rotate=AsyncMock(return_value=SECRET))
        self.batch = AccountPoolBatchService(self.sessions, self.totp, encryption_service)
        self.service_patch = patch('app.routes.account_pool_batch.batch_service', self.batch)
        self.service_patch.start()

    async def asyncTearDown(self):
        self.service_patch.stop()
        await self.engine.dispose()

    async def add(self, content, rotate=True):
        tasks = BackgroundTasks()
        async with self.sessions() as session:
            response = await add_account_pool_emails(
                AccountPoolAddRequest(content=content, rotate_2fa=rotate), tasks,
                db=session, current_user={'username': 'admin'},
            )
        return response.status_code, json.loads(response.body), tasks

    async def jobs(self):
        async with self.sessions() as session:
            return list((await session.execute(select(AccountPoolTotpJob))).scalars())

    async def test_import_queues_only_new_and_restored_accounts_with_credentials(self):
        async with self.sessions() as session:
            session.add_all([
                AccountPoolEntry(email='existing@example.com'),
                AccountPoolEntry(email='restored@example.com', deleted_at=get_now()),
            ])
            await session.commit()
        status, result, tasks = await self.add('\n'.join([
            record('new@example.com'), record('restored@example.com'),
            record('existing@example.com'), 'plain@example.com', 'invalid',
        ]))
        self.assertEqual(status, 200)
        self.assertEqual(result['rotation']['queued'], 2)
        self.assertEqual(result['rotation']['skipped'], [
            {'email': 'plain@example.com', 'reason': '缺少登录密码或原 2FA 密钥'}
        ])
        self.assertEqual(result['existing'], ['existing@example.com'])
        self.assertEqual(result['updated'], [])
        jobs = await self.jobs()
        self.assertEqual({job.email for job in jobs}, {'new@example.com', 'restored@example.com'})
        self.assertTrue(all(job.status == 'pending' for job in jobs))
        self.totp.rotate.assert_not_awaited()
        self.assertEqual(len(tasks.tasks), 1)
        await tasks()
        self.assertEqual(self.totp.rotate.await_count, 2)
        self.assertTrue(all(job.status == 'completed' for job in await self.jobs()))

    async def test_repeat_import_does_not_rotate_existing_account_again(self):
        await self.add(record('new@example.com'))
        async with self.sessions() as session:
            entry = (await session.execute(select(AccountPoolEntry))).scalar_one()
            entry.two_factor_secret_encrypted = encryption_service.encrypt_token('ROTATEDSECRET')
            await session.commit()
        _, repeated, tasks = await self.add(record('new@example.com'))
        self.assertIsNone(repeated['rotation']['batch_id'])
        self.assertEqual(repeated['rotation']['queued'], 0)
        self.assertEqual(tasks.tasks, [])
        self.assertEqual(len(await self.jobs()), 1)
        async with self.sessions() as session:
            entry = (await session.execute(select(AccountPoolEntry))).scalar_one()
            self.assertEqual(encryption_service.decrypt_token(entry.two_factor_secret_encrypted), 'ROTATEDSECRET')

    async def test_opt_out_and_api_default_only_add_accounts(self):
        self.assertFalse(AccountPoolAddRequest().rotate_2fa)
        _, result, tasks = await self.add(record('new@example.com'), rotate=False)
        self.assertTrue(result['success'])
        self.assertNotIn('rotation', result)
        self.assertEqual(tasks.tasks, [])
        self.assertEqual(await self.jobs(), [])
        _, updated, _ = await self.add(record('new@example.com').replace('example-password', 'updated-password'), rotate=False)
        self.assertEqual(updated['updated'], ['new@example.com'])

    async def test_missing_credentials_still_imports_and_reports_skip(self):
        _, result, tasks = await self.add('plain@example.com')
        self.assertEqual(result['added'], ['plain@example.com'])
        self.assertIsNone(result['rotation']['batch_id'])
        self.assertEqual(len(result['rotation']['skipped']), 1)
        self.assertEqual(tasks.tasks, [])

    async def test_queue_failure_preserves_import_and_reports_separate_error(self):
        with patch.object(self.batch, 'enqueue_import_rotations', new=AsyncMock(side_effect=RuntimeError('queue unavailable'))):
            with self.assertLogs('app.routes.admin', level='ERROR'):
                status, result, tasks = await self.add(record('new@example.com'))
        self.assertEqual(status, 200)
        self.assertTrue(result['success'])
        self.assertIn('rotation_error', result)
        self.assertEqual(tasks.tasks, [])
        async with self.sessions() as session:
            entry = (await session.execute(select(AccountPoolEntry))).scalar_one()
            self.assertEqual(entry.email, 'new@example.com')
            self.assertIsNotNone(entry.password_encrypted)

    async def test_invalid_import_does_not_create_rotation_tasks(self):
        status, result, tasks = await self.add('invalid')
        self.assertEqual(status, 400)
        self.assertFalse(result['success'])
        self.assertNotIn('rotation', result)
        self.assertEqual(tasks.tasks, [])
        self.assertEqual(await self.jobs(), [])

    async def test_import_queue_deduplicates_emails_and_skips_active_job(self):
        await self.add(record('new@example.com'), rotate=False)
        async with self.sessions() as session:
            first = await self.batch.enqueue_import_rotations(session, ['new@example.com', 'new@example.com'])
            second = await self.batch.enqueue_import_rotations(session, ['new@example.com'])
        self.assertEqual(first['queued'], 1)
        self.assertEqual(second['queued'], 0)
        self.assertIn('进行中', second['skipped'][0]['reason'])
        self.assertEqual(len(await self.jobs()), 1)


if __name__ == '__main__':
    unittest.main()
