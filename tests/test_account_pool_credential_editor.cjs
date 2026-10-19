const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');

const SECRET = 'JBSWY3DPEHPK3PXP';
const credentials = email => ({email, password: 'old-password', two_factor_secret: SECRET});
const deferred = () => {
    let resolve;
    const promise = new Promise(done => {resolve = done;});
    return {promise, resolve};
};

function runtime() {
    const ids = ['Form', 'Fields', 'Status', 'Password', 'TwoFactor'];
    const elements = Object.fromEntries(ids.map(id => [id, {dataset: {}, value: '', defaultValue: ''}]));
    const calls = [], toasts = [];
    const context = vm.createContext({
        document: {
            getElementById: id => elements[id.replace('accountPoolCredential', '')],
            addEventListener() {},
        },
        window: {accountPoolCredentialStore: {
            load: async email => credentials(email), clear: email => calls.push(['clear', email]),
        }},
        showToast: (...args) => toasts.push(args),
        closeAccountPoolDetails: () => {calls.push(['close']); context.resetAccountPoolCredentialEditor();},
        refreshAccountPoolTable: async () => calls.push(['refresh']),
        fetch: async (url, options) => {
            calls.push(['save', url, JSON.parse(options.body)]);
            return {ok: true, json: async () => ({success: true, message: '已保存', data: credentials('a@example.com')})};
        },
    });
    vm.runInContext(fs.readFileSync(`${__dirname}/../app/static/js/account-pool-credential-editor.js`, 'utf8'), context);
    const load = (entryId = 1) => context.loadAccountPoolCredentialEditor({
        entryId, email: `${entryId}@example.com`, signal: new AbortController().signal,
    });
    const submit = () => context.submitAccountPoolCredentialForm({preventDefault() {}});
    return {context, elements, calls, toasts, load, submit};
}

test('opening more loads fresh credentials, blocks editing until ready and clears plaintext on close', async () => {
    const env = runtime();
    const pending = deferred();
    env.context.window.accountPoolCredentialStore.load = (email, options) => {
        assert.equal(options.fresh, true);
        return pending.promise;
    };
    const loading = env.load();
    assert.equal(env.elements.Form.hidden, false);
    assert.equal(env.elements.Fields.disabled, true);
    await env.submit();
    assert.equal(env.calls.length, 0);
    pending.resolve(credentials('1@example.com'));
    await loading;
    assert.equal(env.elements.Password.value, 'old-password');
    assert.equal(env.elements.TwoFactor.value, SECRET);
    assert.equal(env.elements.Fields.disabled, false);
    env.context.resetAccountPoolCredentialEditor();
    assert.equal(env.elements.Password.value, '');
    assert.equal(env.elements.Password.defaultValue, '');
    assert.equal(env.elements.TwoFactor.value, '');
    assert.equal(env.elements.TwoFactor.defaultValue, '');
    assert.equal(env.elements.Form.hidden, true);
});

test('switching accounts discards a late credential response', async () => {
    const env = runtime();
    const pending = deferred();
    env.context.window.accountPoolCredentialStore.load = () => pending.promise;
    const first = env.load(1);
    env.context.window.accountPoolCredentialStore.load = async () => ({...credentials('2@example.com'), password: 'second'});
    await env.load(2);
    pending.resolve({...credentials('1@example.com'), password: 'first'});
    await first;
    assert.equal(env.elements.Form.dataset.entryId, '2');
    assert.equal(env.elements.Password.value, 'second');
});

test('a failed read exposes the error and cannot save empty or stale fields', async () => {
    const env = runtime();
    env.context.window.accountPoolCredentialStore.load = async () => {throw new Error('凭据解密失败');};
    await env.load();
    await env.submit();
    assert.equal(env.elements.Status.textContent, '凭据解密失败');
    assert.equal(env.elements.Fields.disabled, true);
    assert.deepEqual(env.calls, []);
});

test('saving a password preserves spaces, omits unchanged 2FA and refreshes after success', async () => {
    const env = runtime();
    await env.load(7);
    env.elements.Password.value = ' new-password ';
    await env.submit();
    assert.deepEqual(env.calls, [
        ['save', '/admin/account-pool/7/credentials', {password: ' new-password '}],
        ['clear', 'a@example.com'], ['close'], ['refresh'],
    ]);
});

test('saving only 2FA trims surrounding spaces and omits the password', async () => {
    const env = runtime();
    await env.load();
    env.elements.TwoFactor.value = ' GEZDGNBVGY3TQOJQ ';
    await env.submit();
    assert.deepEqual(env.calls[0][2], {two_factor_secret: 'GEZDGNBVGY3TQOJQ'});
});

test('blank fields retain the saved values and unchanged submissions do not send requests', async () => {
    const env = runtime();
    await env.load();
    await env.submit();
    env.elements.Password.value = '';
    env.elements.TwoFactor.value = '';
    await env.submit();
    assert.deepEqual(env.calls, []);
    assert.equal(env.toasts.length, 2);
    env.elements.Password.value = 'new-password';
    await env.submit();
    assert.deepEqual(env.calls[0][2], {password: 'new-password'});
});

test('invalid 2FA keeps edits visible and allows retry without clearing credentials or refreshing', async () => {
    const env = runtime();
    await env.load();
    env.elements.TwoFactor.value = 'INVALID*SECRET';
    env.context.fetch = async () => ({ok: false, json: async () => ({success: false, error: '密钥必须为 Base32'})});
    await env.submit();
    assert.equal(env.elements.Status.textContent, '密钥必须为 Base32');
    assert.equal(env.elements.TwoFactor.value, 'INVALID*SECRET');
    assert.equal(env.elements.Fields.disabled, false);
    assert.equal(env.elements.Form.hidden, false);
    assert.deepEqual(env.calls, []);
});

test('a pending save cannot be submitted twice or close a newly opened account', async () => {
    const env = runtime();
    await env.load();
    env.elements.Password.value = 'new-password';
    const pending = deferred();
    let saves = 0;
    env.context.fetch = () => {saves++; return pending.promise;};
    const saving = env.submit();
    await env.submit();
    assert.equal(saves, 1);
    await env.load(2);
    pending.resolve({ok: true, json: async () => ({success: true, data: credentials('1@example.com')})});
    await saving;
    assert.equal(env.elements.Form.dataset.entryId, '2');
    assert.equal(env.elements.Form.hidden, false);
    assert.deepEqual(env.calls, [['clear', '1@example.com'], ['refresh']]);
});

test('an owner without a pool entry can fill and save previously empty password and 2FA', async () => {
    const env = runtime();
    env.context.window.accountPoolCredentialStore.load = async email => ({
        email, password: '', two_factor_secret: '',
    });
    await env.load(-3);
    assert.equal(env.elements.Form.hidden, false);
    assert.equal(env.elements.Fields.disabled, false);
    assert.match(env.elements.Status.textContent, /尚未保存/);
    env.elements.Password.value = 'owner-password';
    env.elements.TwoFactor.value = SECRET;
    await env.submit();
    assert.deepEqual(env.calls[0], ['save', '/admin/account-pool/-3/credentials', {
        password: 'owner-password', two_factor_secret: SECRET,
    }]);
    assert.ok(env.calls.some(([action]) => action === 'refresh'));
});
