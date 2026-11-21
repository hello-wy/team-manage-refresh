"""Regression coverage for password choice vs post-auth email verification."""
import json
import unittest
from unittest.mock import AsyncMock, patch

from app.services.openai_auth_email import email_login_options, is_email_verification
from app.services.openai_auth_protocol import inspect_auth_page_session
from app.services.openai_automatic_login import (
    AutomaticLoginDependencies, OpenAIAutomaticLoginError, OpenAIAutomaticLoginService,
)
from tests import test_openai_automatic_login as login_tests
from tests.test_openai_automatic_login import FakeSession, FakeResponse

CALLBACK = 'http://localhost:1455/auth/callback?code=test-code&state=test-state'


def email_page(mode='passwordless_login', **fields):
    session = {'email_verification_mode': mode, **fields}
    if mode == 'passwordless_login' and 'login_methods' not in fields:
        session['login_methods'] = [{'type': 'password'}, {'type': 'email_otp'}]
    doc = {'loaderData': {'routes/layouts/client-auth-session-layout/layout': {'session': session}}}
    return FakeResponse(200, text=f'<script>streamController.enqueue({json.dumps(json.dumps(doc))})</script>')


class EmailModeTests(unittest.TestCase):
    def test_route_loader_overrides_layout_and_cookie(self):
        session = FakeSession([], [], auth_claims={'email_verification_mode': 'passwordless_login'})
        document = {'loaderData': {
            'routes/layouts/client-auth-session-layout/layout': {
                'session': {'email_verification_mode': 'passwordless_login'}},
            'EMAIL_VERIFICATION': {'session': {'email_verification_mode': 'post_auth_email_verification'}},
        }}
        html = f'<script>streamController.enqueue({json.dumps(json.dumps(document))})</script>'
        mode, available = email_login_options(session, FakeResponse(200, text=html))
        self.assertEqual(mode, 'post_auth_email_verification')
        self.assertFalse(available)

    def test_flattened_react_loader_session(self):
        values = [{'_1': 2}, 'loaderData', {'_3': 4},
                  'routes/layouts/client-auth-session-layout/layout', {'_5': 6}, 'session',
                  {'_7': 8, '_9': 10}, 'email_verification_mode', 'passwordless_login',
                  'login_methods', [11], {'_12': 13}, 'type', 'password']
        html = f'<script>streamController.enqueue({json.dumps(json.dumps(values))})</script>'
        self.assertEqual(inspect_auth_page_session(html)['login_methods'], [{'type': 'password'}])
        self.assertEqual(email_login_options(FakeSession([], []), FakeResponse(200, text=html)),
                         ('passwordless_login', True))

    def test_password_link_must_be_a_same_origin_login_link(self):
        for href, expected in [('/log-in/password', True),
                               ('https://auth.openai.com/log-in/password', True),
                               ('https://evil.example/log-in/password', False),
                               ('/create-account/password', False),
                               ('/reset-password', False),
                               ('http://auth.openai.com/log-in/password', False)]:
            with self.subTest(href=href):
                result = email_login_options(FakeSession([], []), FakeResponse(200, text=f'<a href="{href}">Password</a>'))
                self.assertEqual(result[1], expected)

    def test_feature_flags_and_password_text_do_not_create_login_alternative(self):
        html = '<script>const show_contact_verification_password_cta=true;</script>Continue with password'
        self.assertEqual(email_login_options(FakeSession([], []), FakeResponse(200, text=html)), ('unknown', False))

    def test_malformed_session_mode_does_not_crash(self):
        self.assertEqual(email_login_options(FakeSession([], []), email_page([])), ('unknown', False))

    def test_legacy_passwordless_session_without_methods_can_use_password_route(self):
        self.assertEqual(email_login_options(FakeSession([], []), email_page(login_methods=None)),
                         ('passwordless_login', True))

    def test_required_email_and_reauth_are_not_overridden_by_password_link(self):
        for mode, fields in [('post_auth_email_verification', {}), ('passwordless_signup', {}),
                             ('passwordless_login', {'reauth': True}),
                             ('passwordless_login', {'passwordless_otp_from_password_redirect': True}),
                             ('passwordless_login', {'login_methods': [{'type': 'passkey'}]})]:
            with self.subTest(mode=mode, fields=fields):
                response = email_page(mode, **fields)
                response.text += '<a href="/log-in/password">Password</a>'
                self.assertFalse(email_login_options(FakeSession([], []), response)[1])

    def test_email_path_is_checked_without_query_substring_false_positive(self):
        self.assertTrue(is_email_verification('https://auth.openai.com/email-verification?x=1'))
        self.assertFalse(is_email_verification(CALLBACK + '&from=email-verification'))
        self.assertFalse(is_email_verification('https://auth.openai.com/workspace?next=email-verification'))


class PasswordChoiceFlowTests(unittest.IsolatedAsyncioTestCase):
    def service(self, session):
        self.exchange = AsyncMock(return_value={'success': True})
        self.clear = AsyncMock()
        return OpenAIAutomaticLoginService(AutomaticLoginDependencies(
            get_session=AsyncMock(return_value=session), clear_session=self.clear,
            exchange_code=self.exchange, issue_sentinel=AsyncMock(return_value='sentinel'),
        ))

    def request(self):
        return login_tests.OpenAIAutomaticLoginTests().request()

    async def test_email_first_choice_switches_password_for_login_and_liveness(self):
        for method in ['login', 'verify_credentials']:
            with self.subTest(method=method):
                session = FakeSession(
                    [FakeResponse(200), email_page(), FakeResponse(200)],
                    [FakeResponse(200, payload={'continue_url': '/email-verification'}),
                     FakeResponse(200, payload={'continue_url': CALLBACK})],
                )
                service = self.service(session)
                await getattr(service, method)(self.request())
                self.assertEqual([url.rsplit('/', 1)[-1] for url, _ in session.posts], ['continue', 'verify'])
                self.assertEqual(session.post_headers[-1]['Referer'], 'https://auth.openai.com/log-in/password')
                self.assertEqual(session.post_headers[-1]['openai-sentinel-token'], 'sentinel')
                self.assertEqual(session.get_responses, [])
                if method == 'verify_credentials':
                    self.exchange.assert_not_awaited()
                    self.assertEqual(self.clear.await_count, 2)
                else:
                    self.exchange.assert_awaited_once()

    async def test_post_password_alternative_is_followed_once_for_both_flows(self):
        for method in ['login', 'verify_credentials']:
            with self.subTest(method=method):
                session = FakeSession(
                    [FakeResponse(200), FakeResponse(200), email_page(), FakeResponse(200)],
                    [FakeResponse(200, payload={'continue_url': '/log-in/password'}),
                     FakeResponse(200, payload={'continue_url': '/email-verification'}),
                     FakeResponse(200, payload={'continue_url': CALLBACK})],
                )
                await getattr(self.service(session), method)(self.request())
                self.assertEqual(sum('/password/verify' in url for url, _ in session.posts), 2)

    async def test_password_choice_still_completes_totp_before_token_exchange(self):
        session = FakeSession(
            [FakeResponse(200), email_page(), FakeResponse(200)],
            [FakeResponse(200, payload={'continue_url': '/email-verification'}),
             FakeResponse(200, payload={
                 'continue_url': '/mfa-challenge/factor', 'page': {'type': 'mfa_challenge'},
                 'oai-client-auth-session': {'mfa_challenge_factors': [{'factor_type': 'totp', 'id': 'factor'}]},
             }), FakeResponse(200), FakeResponse(200, payload={'continue_url': CALLBACK})],
        )
        service = self.service(session)
        with patch('app.services.openai_auth_totp.generate_totp', return_value='123456'):
            result = await service.login(self.request())
        self.assertEqual(result['verified_totp_factor_id'], 'factor')
        self.assertEqual(session.posts[-1][1], {'type': 'totp', 'id': 'factor', 'code': '123456'})
        self.exchange.assert_awaited_once()

    async def test_callback_query_containing_email_page_name_does_not_block_success(self):
        session = FakeSession(
            [FakeResponse(200), FakeResponse(200)],
            [FakeResponse(200, payload={'continue_url': '/log-in/password'}),
             FakeResponse(200, payload={'continue_url': CALLBACK + '&from=email-verification'})],
        )
        service = self.service(session)
        self.assertTrue((await service.login(self.request()))['success'])

    async def test_required_post_auth_email_stops_without_reposting_password_or_exchanging(self):
        for method in ['login', 'verify_credentials']:
            with self.subTest(method=method):
                session = FakeSession(
                    [FakeResponse(200), FakeResponse(200), email_page('post_auth_email_verification', email_verified=False)],
                    [FakeResponse(200, payload={'continue_url': '/log-in/password'}),
                     FakeResponse(200, payload={'continue_url': '/email-verification'})],
                )
                service = self.service(session)
                with self.assertRaisesRegex(OpenAIAutomaticLoginError, 'OAuth 授权尚未完成'):
                    await getattr(service, method)(self.request())
                self.assertEqual(len(session.posts), 2)
                self.exchange.assert_not_awaited()

    async def test_repeated_email_choice_does_not_loop(self):
        session = FakeSession(
            [FakeResponse(200), FakeResponse(200), email_page(), FakeResponse(200), email_page()],
            [FakeResponse(200, payload={'continue_url': '/log-in/password'}),
             FakeResponse(200, payload={'continue_url': '/email-verification'}),
             FakeResponse(200, payload={'continue_url': '/email-verification'})],
        )
        service = self.service(session)
        with self.assertRaisesRegex(OpenAIAutomaticLoginError, '自动登录已停止'):
            await service.login(self.request())
        self.assertEqual(len(session.posts), 3)
        self.exchange.assert_not_awaited()

    async def test_blocked_password_page_never_receives_password(self):
        session = FakeSession(
            [FakeResponse(200), email_page(), FakeResponse(403)],
            [FakeResponse(200, payload={'continue_url': '/email-verification'})],
        )
        service = self.service(session)
        with self.assertRaisesRegex(OpenAIAutomaticLoginError, '未允许切换'):
            await service.login(self.request())
        self.assertEqual(len(session.posts), 1)
        self.exchange.assert_not_awaited()

    async def test_unknown_email_challenge_is_not_assumed_to_allow_password(self):
        session = FakeSession(
            [FakeResponse(200), FakeResponse(200)],
            [FakeResponse(200, payload={'continue_url': '/email-verification'})],
        )
        with self.assertRaisesRegex(OpenAIAutomaticLoginError, '未提供可切换'):
            await self.service(session).verify_credentials(self.request())
        self.assertEqual(len(session.posts), 1)


if __name__ == '__main__':
    unittest.main()
