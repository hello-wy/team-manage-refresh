import hashlib
import hmac
import json
import re
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base, get_db
from app.dependencies.auth import require_admin
from app.main import app
from app.models import AccountPoolEntry, RotationAction, Team, TeamEmailMapping
from app.services.encryption import encryption_service
from app.services.settings import settings_service
from app.services.stock_16688 import (
    DEFAULT_GATEWAY, Stock16688Error, Stock16688Service, canonical_value,
    load_config, normalize_gateway, public_settings, save_settings, sign_params,
)
from app.utils.time_utils import get_now

EMAIL = 'member@example.com'
PASSWORD = 'pass&word=测试'
TOTP = 'JBSWY3DPEHPK3PXP'
SECRET = 'private-app-secret'


class Stock16688Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        settings_service.clear_cache()
        self.engine = create_async_engine('sqlite+aiosqlite:///:memory:')
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.db = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=False)()
        self.entry = AccountPoolEntry(email=EMAIL,
            password_encrypted=encryption_service.encrypt_token(PASSWORD),
            two_factor_secret_encrypted=encryption_service.encrypt_token(TOTP))
        self.team = Team(email='owner@example.com', account_id='space', access_token_encrypted='unused')
        self.db.add_all([self.entry, self.team])
        await self.db.flush()
        self.mapping = TeamEmailMapping(team_id=self.team.id, email=EMAIL, status='invited', source='admin_add')
        self.db.add(self.mapping)
        await self.db.commit()
        await save_settings(self.db, gateway=DEFAULT_GATEWAY, app_id='test-app', goods_no='G10001', secret=SECRET)
        self.sent = []
        self.result = {'code': 1, 'data': {'goods_no': 'G10001', 'import_count': 1, 'stock_dup_count': 0}}
        def handler(request):
            self.sent.append(request)
            return httpx.Response(200, json=self.result)
        self.service = Stock16688Service(lambda **kwargs: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), **kwargs))

    async def asyncTearDown(self):
        await self.db.close()
        await self.engine.dispose()
        settings_service.clear_cache()

    async def test_signed_restock_sends_exact_three_dash_format(self):
        with patch('app.services.stock_16688.time.time', return_value=1710000000), patch(
            'app.services.stock_16688.secrets.token_hex', return_value='a' * 32):
            result = await self.service.import_entry(self.db, self.entry.id)
        self.assertEqual(result['import_count'], 1)
        self.assertEqual(self.sent[0].url, DEFAULT_GATEWAY + '/stockSecretBatch/add')
        params = json.loads(self.sent[0].content)
        self.assertEqual(params['content'], f'{EMAIL}---{PASSWORD}---{TOTP}')
        self.assertEqual(params['timestamp'], '1710000000')
        self.assertEqual(params['nonce'], 'a' * 32)
        self.assertEqual((params['split_type'], params['remove_repeat']), (1, 1))
        plain = ('POST\nstocksecretbatch\nadd\napp_id=test-app&content=' + f'{EMAIL}---{PASSWORD}---{TOTP}'
                 + '&expire_time=0&first=0&goods_no=G10001&nonce=' + 'a' * 32
                 + '&remove_repeat=1&split_type=1&timestamp=1710000000')
        expected = hmac.new(SECRET.encode(), plain.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(params['sign'], expected)
        self.assertNotIn(PASSWORD, json.dumps(result, ensure_ascii=False))
        self.assertNotIn(TOTP, json.dumps(result))
        self.assertEqual(self.mapping.status, 'invited')

    async def test_inventory_duplicate_is_success_without_new_stock(self):
        self.result['data'].update(import_count=0, stock_dup_count=1)
        result = await self.service.import_entry(self.db, self.entry.id)
        self.assertTrue(result['success'])
        self.assertIn('无需重复', result['message'])

    async def test_changed_state_owner_conflict_and_open_actions_are_rejected(self):
        self.mapping.status = 'joined'
        await self.db.commit()
        with self.assertRaisesRegex(Stock16688Error, '待接受邀请'):
            await self.service.import_entry(self.db, self.entry.id)
        self.mapping.status = 'invited'
        self.team.email = EMAIL
        await self.db.commit()
        with self.assertRaisesRegex(Stock16688Error, '待接受邀请'):
            await self.service.import_entry(self.db, self.entry.id)
        self.team.email = 'owner@example.com'
        action = RotationAction(team_id=self.team.id, email=EMAIL, action_type='invite', status='pending', idempotency_key='p')
        self.db.add(action)
        await self.db.commit()
        with self.assertRaisesRegex(Stock16688Error, '待接受邀请'):
            await self.service.import_entry(self.db, self.entry.id)
        await self.db.delete(action)
        other = Team(email='other@example.com', account_id='other', access_token_encrypted='unused')
        self.db.add(other)
        await self.db.flush()
        self.db.add(TeamEmailMapping(team_id=other.id, email=EMAIL, status='invited'))
        await self.db.commit()
        with self.assertRaisesRegex(Stock16688Error, '待接受邀请'):
            await self.service.import_entry(self.db, self.entry.id)
        self.assertEqual(self.sent, [])

    async def test_missing_deleted_and_virtual_accounts_cannot_export(self):
        for entry_id in (999, -self.team.id):
            with self.assertRaisesRegex(Stock16688Error, '不存在'):
                await self.service.import_entry(self.db, entry_id)
        self.entry.deleted_at = get_now()
        await self.db.commit()
        with self.assertRaisesRegex(Stock16688Error, '已删除'):
            await self.service.import_entry(self.db, self.entry.id)
        self.assertEqual(self.sent, [])

    async def test_missing_password_or_2fa_never_calls_vendor(self):
        password = self.entry.password_encrypted
        self.entry.password_encrypted = None
        await self.db.commit()
        with self.assertRaisesRegex(Stock16688Error, '缺少密码或 2FA'):
            await self.service.import_entry(self.db, self.entry.id)
        self.entry.password_encrypted = password
        self.entry.two_factor_secret_encrypted = None
        await self.db.commit()
        with self.assertRaisesRegex(Stock16688Error, '缺少密码或 2FA'):
            await self.service.import_entry(self.db, self.entry.id)
        self.assertEqual(self.sent, [])

    async def test_card_delimiters_and_line_breaks_cannot_create_extra_stock(self):
        for password in ('one\ntwo', 'one\rtwo', 'one---two'):
            self.entry.password_encrypted = encryption_service.encrypt_token(password)
            await self.db.commit()
            with self.assertRaisesRegex(Stock16688Error, '分隔符'):
                await self.service.import_entry(self.db, self.entry.id)
        self.assertEqual(self.sent, [])

    async def test_vendor_error_never_echoes_credentials(self):
        self.result = {'code': 0, 'msg': f'failed content: {EMAIL}---{PASSWORD}---{TOTP} {SECRET}'}
        with self.assertRaises(Stock16688Error) as raised:
            await self.service.import_entry(self.db, self.entry.id)
        self.assertNotIn(PASSWORD, str(raised.exception))
        self.assertNotIn(TOTP, str(raised.exception))
        self.assertNotIn(SECRET, str(raised.exception))

    async def test_timeout_does_not_retry_and_marks_result_as_uncertain(self):
        calls = []
        def timeout(request):
            calls.append(request)
            raise httpx.ReadTimeout('sensitive request must not be echoed', request=request)
        service = Stock16688Service(lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(timeout), **kwargs))
        with self.assertRaisesRegex(Stock16688Error, '可能已入库'):
            await service.import_entry(self.db, self.entry.id)
        self.assertEqual(len(calls), 1)

    async def test_redirect_html_and_malformed_success_do_not_report_success(self):
        responses = [httpx.Response(302, headers={'Location': 'https://other.example'}),
                     httpx.Response(200, text='<html>login</html>'), httpx.Response(503, text=PASSWORD)]
        for response in responses:
            requests = []
            def handler(request):
                requests.append(request)
                return response
            service = Stock16688Service(lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs))
            with self.assertRaises(Stock16688Error):
                await service.import_entry(self.db, self.entry.id)
            self.assertEqual(len(requests), 1)
        for data in ({}, {'goods_no': 'other', 'import_count': 1, 'stock_dup_count': 0},
                     {'goods_no': 'G10001', 'import_count': 0, 'stock_dup_count': 0},
                     {'goods_no': 'G10001', 'import_count': True, 'stock_dup_count': 0}):
            self.result = {'code': 1, 'data': data}
            with self.assertRaisesRegex(Stock16688Error, '未确认'):
                await self.service.import_entry(self.db, self.entry.id)

    async def test_settings_encrypt_secret_and_blank_update_preserves_it(self):
        encrypted = await settings_service.get_setting(self.db, 'stock_16688_secret_encrypted')
        self.assertNotIn(SECRET, encrypted)
        await save_settings(self.db, gateway='https://www.16688.com.cn/', app_id='app-two', goods_no='G20002', secret='')
        config = await load_config(self.db)
        self.assertEqual(config.secret, SECRET)
        self.assertEqual(config.goods_no, 'G20002')
        view = await public_settings(self.db)
        self.assertTrue(view['stock_16688_has_secret'])
        self.assertNotIn(SECRET, json.dumps(view))
        self.assertNotIn(encrypted, json.dumps(view))

    async def test_unconfigured_secret_blocks_export(self):
        await settings_service.update_setting(self.db, 'stock_16688_secret_encrypted', '')
        with self.assertRaisesRegex(Stock16688Error, '系统设置'):
            await self.service.import_entry(self.db, self.entry.id)
        with self.assertRaisesRegex(Stock16688Error, 'Secret'):
            await save_settings(self.db, gateway=DEFAULT_GATEWAY, app_id='a', goods_no='g', secret='')
        self.assertEqual(self.sent, [])

    async def test_all_rows_show_icon_but_only_pending_invites_with_credentials_can_import(self):
        from app.main import templates
        from app.services.account_pool import account_pool_service
        from app.services.account_pool_rotation import attach_rotation_status
        for email, member_status in (('joined@example.com', 'joined'), ('owner@example.com', 'joined'),
                                     ('missing@example.com', 'invited')):
            self.db.add(AccountPoolEntry(email=email))
            self.db.add(TeamEmailMapping(team_id=self.team.id, email=email, status=member_status))
        await self.db.commit()
        listing = await account_pool_service.list_entries(self.db)
        listing['entries'] = await attach_rotation_status(self.db, listing['entries'])
        html = templates.get_template('admin/account_pool/index.html').render(
            **listing, user={'username': 'admin'}, admin_profile={'nickname': '管理员'},
            active_page='account_pool', filter_url=lambda **kwargs: '#')
        buttons = re.findall(r'<button\b[^>]*class="[^"]*\baccount-pool-16688-button\b[^"]*"[^>]*>', html)
        self.assertEqual(len(buttons), 4)
        for email in (EMAIL, 'joined@example.com', 'owner@example.com', 'missing@example.com'):
            button = next(button for button in buttons if f'将 {email} 导入 16688' in button)
            if email == EMAIL:
                self.assertNotRegex(button, r'\bdisabled\b')
            else:
                self.assertRegex(button, r'\bdisabled\b')
        self.assertEqual(html.count('src="/static/stock-16688-icon.png"'), 4)

    def test_gateway_and_canonical_signature_follow_documentation(self):
        self.assertEqual(normalize_gateway('https://www.16688.com.cn'), DEFAULT_GATEWAY)
        for value in ('http://www.16688.com.cn', 'https://user:pass@example.com', 'https://example.com/other',
                      'https://example.com?api=1', 'https://example.com:bad', 'https://exa mple.com'):
            with self.assertRaises(Stock16688Error):
                normalize_gateway(value)
        self.assertEqual(canonical_value({'z': [False, {'b': '中/文', 'a': ''}], 'a': True}),
                         '{"a":true,"z":[false,{"a":"","b":"中/文"}]}')
        self.assertEqual(sign_params({'z': '', 'a': False, 'sign': 'ignored'}, 'k'),
            hmac.new(b'k', b'POST\nstocksecretbatch\nadd\na=false&z=', hashlib.sha256).hexdigest())


class Stock16688RouteTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        async def database():
            yield AsyncMock()
        app.dependency_overrides[get_db] = database

    def tearDown(self):
        app.dependency_overrides.clear()
        self.client.close()

    def test_endpoints_require_admin(self):
        for path, payload in (('/admin/account-pool/1/import-16688', None),
            ('/admin/settings/16688', {'app_id': 'a', 'goods_no': 'g', 'secret': SECRET})):
            response = self.client.post(path, json=payload)
            self.assertIn(response.status_code, (401, 403))

    def test_import_endpoint_has_no_store_and_only_safe_response(self):
        app.dependency_overrides[require_admin] = lambda: {'is_admin': True}
        with patch('app.routes.stock_16688.stock_16688_service.import_entry', new=AsyncMock(return_value={
            'success': True, 'message': '已成功补货至 16688', 'import_count': 1, 'stock_dup_count': 0})) as send:
            response = self.client.post('/admin/account-pool/7/import-16688')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(send.await_args.args[1], 7)
        self.assertNotIn(PASSWORD, response.text)

    def test_changed_state_returns_conflict(self):
        app.dependency_overrides[require_admin] = lambda: {'is_admin': True}
        with patch('app.routes.stock_16688.stock_16688_service.import_entry', new=AsyncMock(
            side_effect=Stock16688Error('请刷新号池', 409))):
            response = self.client.post('/admin/account-pool/7/import-16688')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.headers['cache-control'], 'no-store')

    def test_settings_endpoint_never_returns_secret(self):
        app.dependency_overrides[require_admin] = lambda: {'is_admin': True}
        with patch('app.routes.stock_16688.save_settings', new=AsyncMock(return_value={
            'success': True, 'message': '已保存', 'gateway': DEFAULT_GATEWAY})) as save:
            response = self.client.post('/admin/settings/16688', json={
                'app_id': 'my-app', 'goods_no': 'G10001', 'secret': SECRET})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(save.await_args.kwargs['secret'], SECRET)
        self.assertNotIn(SECRET, response.text)

    def test_invalid_gateway_is_rejected_before_saving_configuration(self):
        app.dependency_overrides[require_admin] = lambda: {'is_admin': True}
        response = self.client.post('/admin/settings/16688', json={
            'gateway': 'https://example.com/not-an-api', 'app_id': 'my-app', 'goods_no': 'G10001', 'secret': SECRET})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(SECRET, response.text)
