const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const {test} = require('node:test');
const {harness} = require('./helpers/team-import-harness.cjs');

const PARSE_URL = '/admin/oauth/openai/parse-callback';
const IMPORT_URL = '/admin/teams/import';

test('quick import parses and submits fresh credentials in one action', async () => {
    const h = harness();
    assert.equal(h.form.accessToken.disabled, true);
    await h.submit();
    assert.deepEqual(h.calls.map(call => call.url), [PARSE_URL, IMPORT_URL]);
    assert.equal(h.calls[0].body.callback_text, h.callback.value);
    assert.equal(h.calls[0].body.code_verifier, 'test-verifier');
    assert.equal(h.calls[0].body.expected_state, 'test-state');
    assert.deepEqual(h.calls[1].body, {import_type: 'single', access_token: 'new-at',
        id_token: 'new-id', refresh_token: 'new-rt', session_token: null,
        client_id: 'app_new', email: 'new@example.com', account_id: 'new-account', pool_type: 'welfare'});
    assert.equal(h.form.resetCount, 1);
    assert.equal(h.context.refreshCount, 1);
    assert.deepEqual(h.toasts, [['Team 导入成功！', 'success']]);
    assert.equal(h.quickButton.textContent, '导入');
    assert.ok(h.buttons.every(button => !button.disabled));
});

test('empty callback never imports stale credentials', async () => {
    const h = harness();
    h.callback.value = '   ';
    await h.submit();
    assert.equal(h.calls.length, 0);
    assert.deepEqual(h.toasts, [['请先粘贴回调 URL', 'error']]);
    assert.equal(h.form.resetCount, 0);
    assert.equal(h.quickButton.disabled, false);
});

for (const parseResult of [
    {success: false, error: 'state 不匹配'},
    {success: true, data: {success: false, error: 'state 不匹配'}},
]) {
    test('parse failure prevents import: transport success ' + parseResult.success, async () => {
        const h = harness();
        h.context.parseResult = parseResult;
        await h.submit();
        assert.deepEqual(h.calls.map(call => call.url), [PARSE_URL]);
        assert.deepEqual(h.toasts, [['state 不匹配', 'error']]);
        assert.equal(h.form.resetCount, 0);
        assert.equal(h.quickButton.disabled, false);
    });
}

test('missing parsed credentials cannot submit stale tokens', async () => {
    const h = harness();
    h.context.parseResult = {success: true, data: {success: true, data: {email: 'new@example.com'}}};
    await h.submit();
    assert.deepEqual(h.calls.map(call => call.url), [PARSE_URL]);
    assert.deepEqual(h.toasts, [['回调解析结果缺少 Token，请重新授权', 'error']]);
});

test('refresh-token-only callback remains supported', async () => {
    const h = harness();
    h.context.parseResult.data.data = {refresh_token: 'rt-only', client_id: 'app_refresh'};
    await h.submit();
    assert.equal(h.calls[1].body.refresh_token, 'rt-only');
    assert.equal(h.calls[1].body.client_id, 'app_refresh');
    assert.equal(h.form.resetCount, 1);
});

test('import retry reuses parsed credentials without redeeming the code again', async () => {
    const h = harness();
    h.context.importResult = {success: false, error: '导入暂时失败'};
    await h.submit();
    assert.equal(h.form.resetCount, 0);
    assert.equal(h.context.refreshCount, 0);
    assert.deepEqual(h.toasts, [['导入暂时失败', 'error']]);
    h.context.importResult = {success: true, data: {success: true}};
    await h.submit();
    assert.deepEqual(h.calls.map(call => call.url), [PARSE_URL, IMPORT_URL, IMPORT_URL]);
    assert.equal(h.form.resetCount, 1);
});

test('changed callback is parsed again after import failure', async () => {
    const h = harness();
    h.context.importResult = {success: false, error: '导入失败'};
    await h.submit();
    h.callback.value = 'http://localhost:1455/auth/callback?code=second&state=test-state';
    h.context.parseResult.data.data = {access_token: 'second-at'};
    await h.submit();
    assert.deepEqual(h.calls.map(call => call.url), [PARSE_URL, IMPORT_URL, PARSE_URL, IMPORT_URL]);
    assert.equal(h.calls[3].body.access_token, 'second-at');
});

test('success clears the parsed callback cache', async () => {
    const h = harness();
    await h.submit();
    await h.submit();
    assert.deepEqual(h.calls.map(call => call.url), [PARSE_URL, IMPORT_URL, PARSE_URL, IMPORT_URL]);
});

test('duplicate submits while parsing issue only one parse and import', async () => {
    const h = harness();
    const parseResult = h.context.parseResult;
    let resolveParse;
    h.context.parseResult = new Promise(resolve => { resolveParse = resolve; });
    const pending = h.submit();
    assert.ok(h.buttons.every(button => button.disabled));
    assert.equal(h.quickButton.textContent, '解析并导入中...');
    await h.submit();
    assert.equal(h.calls.length, 1);
    resolveParse(parseResult);
    await pending;
    assert.deepEqual(h.calls.map(call => call.url), [PARSE_URL, IMPORT_URL]);
});

test('duplicate submits while importing issue only one import', async () => {
    const h = harness();
    let resolveImport;
    h.context.importResult = new Promise(resolve => { resolveImport = resolve; });
    const pending = h.submit();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(h.quickButton.textContent, '导入中...');
    await h.submit();
    resolveImport({success: true, data: {success: true}});
    await pending;
    assert.deepEqual(h.calls.map(call => call.url), [PARSE_URL, IMPORT_URL]);
});

test('application-level import failure does not reset the form', async () => {
    const h = harness();
    h.context.importResult = {success: true, data: {success: false, error: '导入被拒绝'}};
    await h.submit();
    assert.deepEqual(h.toasts, [['导入被拒绝', 'error']]);
    assert.equal(h.form.resetCount, 0);
    assert.equal(h.context.refreshCount, 0);
    assert.equal(h.quickButton.disabled, false);
});

test('manual import submits supplied fields without parsing a callback', async () => {
    const h = harness();
    h.context.setSingleImportMode('manual');
    assert.equal(h.form.accessToken.disabled, false);
    h.callback.value = '';
    await h.submit();
    assert.deepEqual(h.calls.map(call => call.url), [IMPORT_URL]);
    assert.deepEqual(h.calls[0].body, {import_type: 'single', access_token: 'stale-at',
        id_token: 'stale-id', refresh_token: 'stale-rt', session_token: 'stale-session',
        client_id: 'app_test', email: 'stale@example.com', account_id: 'stale-account', pool_type: 'welfare'});
    assert.equal(h.manualButton.textContent, '导入 Team');
});

test('credential import submits email, password and 2FA login payload', async () => {
    const h = harness();
    h.context.setSingleImportMode('credentials');
    await h.submit();
    assert.deepEqual(h.calls.map(call => call.url), [IMPORT_URL]);
    assert.deepEqual(h.calls[0].body, {
        import_type: 'credentials', email: 'owner@example.com', password: 'owner-password',
        two_factor_secret: 'JBSWY3DPEHPK3PXP', account_id: 'owner-team', pool_type: 'welfare',
    });
    assert.equal(h.credentialButton.textContent, '登录并导入 Team');
});

test('credential text preserves password whitespace, symbols and embedded separators', async () => {
    const h = harness();
    h.context.setSingleImportMode('credentials');
    h.form.loginCredentials.value = '  Owner@Example.com ---- p#A----B$! ---- JBSWY3DPEHPK3PXP \n';
    h.form.loginAccountId.value = '';
    await h.submit();
    assert.equal(h.calls[0].body.email, 'Owner@Example.com');
    assert.equal(h.calls[0].body.password, ' p#A----B$! ');
    assert.equal(h.calls[0].body.two_factor_secret, 'JBSWY3DPEHPK3PXP');
    assert.equal(h.calls[0].body.account_id, null);
});

test('credential text accepts an otpauth link without changing its contents', async () => {
    const h = harness();
    h.context.setSingleImportMode('credentials');
    const secret = 'otpauth://totp/Example:owner?secret=JBSWY3DPEHPK3PXP&issuer=Example';
    h.form.loginCredentials.value = `owner@example.com----password----${secret}`;
    await h.submit();
    assert.equal(h.calls[0].body.two_factor_secret, secret);
});

const invalidCredentials = [
    ['', '请按 邮箱----密码----2FA 密钥 格式粘贴一个账号'],
    ['owner@example.com----password', '请按 邮箱----密码----2FA 密钥 格式粘贴一个账号'],
    ['owner@example.com----password----SECRET\nother@example.com----password----SECRET',
        '请按 邮箱----密码----2FA 密钥 格式粘贴一个账号'],
    ['invalid-email----password----SECRET', '账号邮箱格式不正确'],
    ['owner@example.com---- ----SECRET', '账号密码和 2FA 密钥不能为空'],
    ['owner@example.com----password----', '账号密码和 2FA 密钥不能为空'],
];

for (const [value, message] of invalidCredentials) {
    test('invalid credential text prevents login: ' + message + ' (' + value.length + ')', async () => {
        const h = harness();
        h.context.setSingleImportMode('credentials');
        h.form.loginCredentials.value = value;
        await h.submit();
        assert.equal(h.calls.length, 0);
        assert.equal(h.form.resetCount, 0);
        assert.deepEqual(h.toasts, [[message, 'error']]);
        assert.equal(h.credentialButton.disabled, false);
    });
}

test('mode switching enables only the active credential text and account ID', () => {
    const h = harness();
    assert.equal(h.form.loginCredentials.disabled, true);
    h.context.setSingleImportMode('credentials');
    assert.equal(h.form.loginCredentials.disabled, false);
    assert.equal(h.form.loginAccountId.disabled, false);
    assert.equal(h.form.accessToken.disabled, true);
    h.context.setSingleImportMode('manual');
    assert.equal(h.form.loginCredentials.disabled, true);
    assert.equal(h.form.loginAccountId.disabled, true);
});

test('quick import template exposes only one combined import button', () => {
    const html = readFileSync(join(__dirname, '../app/templates/base.html'), 'utf8');
    const section = html.slice(html.indexOf('id="oauthQuickSection"'), html.indexOf('id="manualTokenSection"'));
    assert.doesNotMatch(section, /btnParseOAuthCallback|解析并自动填充/);
    assert.equal((section.match(/type="submit"/g) || []).length, 1);
    assert.ok(section.includes('id="btnQuickImportTeam" class="btn btn-primary">导入</button>'));
    assert.ok(html.indexOf('/static/js/team-import.js?') > html.indexOf('/static/js/main.js?'));
});
