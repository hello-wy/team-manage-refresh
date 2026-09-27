const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');

const script = name => fs.readFileSync(`${__dirname}/../app/static/js/${name}`, 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));

function runtime() {
    const storage = new Map();
    const elements = new Map([
        ['accountPoolEmails', {value: 'new@example.com----password----SECRET'}],
        ['accountPoolSubmitBtn', {disabled: false}],
        ['accountPoolAutoRotate', {checked: true}],
        ['accountPoolBatchProgress', {hidden: true, textContent: ''}],
    ]);
    const cleared = [], toasts = [], timers = [];
    const code = {dataset: {email: 'first@example.com'}, secret: 'OLDSECRET'};
    const context = vm.createContext({
        document: {addEventListener() {}, getElementById: id => elements.get(id), querySelectorAll: () => [code]},
        window: {accountPoolCredentialStore: {clear: email => cleared.push(email)}},
        sessionStorage: {getItem: key => storage.get(key) || null,
            setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key)},
        showToast: (...args) => toasts.push(args),
        setTimeout: callback => timers.push(callback), console,
    });
    vm.runInContext(script('account-pool.js'), context);
    context.refreshAccountPoolTable = async () => {};
    vm.runInContext(script('account-pool-batch.js'), context);
    return {context, elements, storage, cleared, toasts, timers, code};
}

for (const checked of [true, false]) {
    test(`import sends automatic rotation preference: ${checked}`, async () => {
        const {context, elements} = runtime();
        elements.get('accountPoolAutoRotate').checked = checked;
        let posted;
        context.fetch = async (url, options) => {
            posted = JSON.parse(options.body);
            return {ok: true, json: async () => ({success: true, added: ['new@example.com'], message: '已加入'})};
        };
        await context.submitAccountPoolForm({preventDefault() {}});
        assert.equal(posted.rotate_2fa, checked);
        assert.equal(elements.get('accountPoolEmails').value, '');
        assert.equal(elements.get('accountPoolSubmitBtn').disabled, false);
    });
}

test('import success is kept when rotation enqueue fails', async () => {
    const {context, elements, toasts} = runtime();
    context.fetch = async () => ({ok: true, json: async () => ({success: true,
        message: '已加入', rotation_error: '2FA 任务创建失败，请重试'})});
    await context.submitAccountPoolForm({preventDefault() {}});
    assert.equal(elements.get('accountPoolEmails').value, '');
    assert.ok(toasts.some(([message, level]) => message.includes('2FA') && level === 'warning'));
    assert.match(elements.get('accountPoolBatchProgress').textContent, /任务创建失败/);
});

test('multiple rotation batches retain unfinished tracking and aggregate progress', async () => {
    const {context, elements, storage, timers, cleared, code} = runtime();
    let secondDone = false;
    context.fetch = async url => ({ok: true, json: async () => ({results: [
        url.endsWith('/first')
            ? {email: 'first@example.com', status: 'completed', two_factor_secret: 'NEWSECRET'}
            : {email: 'second@example.com', status: secondDone ? 'completed' : 'running'},
    ]})});
    context.startAccountPoolRotationWatch('first');
    context.startAccountPoolRotationWatch('second');
    await flush();
    assert.deepEqual(JSON.parse(storage.get('account_pool_rotation_batches_v2')), ['second']);
    assert.match(elements.get('accountPoolBatchProgress').textContent, /成功 1，失败 0，进行中 1/);
    assert.ok(cleared.includes('first@example.com'));
    assert.equal(code.secret, 'NEWSECRET');
    secondDone = true;
    await timers.shift()();
    await flush();
    assert.deepEqual(JSON.parse(storage.get('account_pool_rotation_batches_v2')), []);
    assert.match(elements.get('accountPoolBatchProgress').textContent, /成功 2，失败 0，进行中 0/);
});

test('skipped imports explain why no rotation is performed', () => {
    const {context, elements} = runtime();
    context.startAccountPoolRotationWatch(null, ['plain@example.com：跳过自动更换 · 缺少登录密码或原 2FA 密钥']);
    assert.match(elements.get('accountPoolBatchProgress').textContent, /跳过自动更换/);
});
