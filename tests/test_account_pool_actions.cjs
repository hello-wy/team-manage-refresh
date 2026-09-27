const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');

function runtime() {
    let dialog;
    let focused = 0;
    const trigger = {isConnected: true, focus() { focused++; }};
    const context = vm.createContext({
        document: {
            activeElement: trigger, body: {appendChild() {}}, addEventListener() {},
            createElement() {
                const handlers = {};
                const controls = Object.fromEntries(['p', '[data-confirm]', '[data-cancel]'].map(key =>
                    [key, {addEventListener(event, fn) { this[event] = fn; }}]));
                dialog = {
                    handlers, controls, setAttribute() {},
                    querySelector(key) { return controls[key]; },
                    addEventListener(event, fn) { handlers[event] = fn; },
                    showModal() { this.open = true; }, close() { this.open = false; },
                    remove() { this.removed = true; },
                };
                return dialog;
            },
        },
        window: {}, Date, console,
        confirm() { throw new Error('Native confirm must never be called'); },
    });
    for (const name of ['account-pool-confirm.js', 'account-pool.js']) {
        vm.runInContext(fs.readFileSync(`${__dirname}/../app/static/js/${name}`, 'utf8'), context);
    }
    return {context, dialog: () => dialog, focused: () => focused};
}

test('page confirmation displays literal text, resolves once and restores focus', async () => {
    const env = runtime();
    const result = env.context.confirmAccountPoolAction('<img src=x>');
    assert.equal(env.dialog().controls.p.textContent, '<img src=x>');
    assert.equal(env.dialog().open, true);
    assert.equal(await env.context.confirmAccountPoolAction('duplicate'), false);
    env.dialog().controls['[data-confirm]'].click();
    assert.equal(await result, true);
    assert.equal(env.focused(), 1);
    env.dialog().handlers.close();
    assert.equal(env.focused(), 1);
});

test('cancel and Escape abort export before any request despite native dialog suppression', async () => {
    for (const escape of [false, true]) {
        const env = runtime();
        env.context.fetch = () => { throw new Error('Must not send a request'); };
        const result = env.context.runAccountPoolAutomaticLogin(1, 'test@example.com', '', {});
        if (escape) env.dialog().handlers.cancel({preventDefault() {}, stopPropagation() {}});
        else env.dialog().controls['[data-cancel]'].click();
        await result;
        assert.equal(env.dialog().removed, true);
        assert.equal(env.focused(), 1);
    }
});

test('scheduled replacement countdown reaches waiting state and respects timezone', () => {
    const {context} = runtime();
    const now = Date.parse('2030-01-01T00:00:00Z');
    assert.equal(context.accountPoolCountdownLabel('2030-01-01T09:02:03+08:00', now), '释放席位倒计时 01:02:03');
    assert.equal(context.accountPoolCountdownLabel('2030-01-01T08:00:00+08:00', now), '下线时间已到，等待成员退出');
    assert.equal(context.accountPoolCountdownLabel('', now), '下线时间待确认');
});

test('quota refresh updates only its cell and re-enables retry after an error', async () => {
    const {context} = runtime();
    const content = {innerHTML: 'old quota'};
    const feedback = {};
    let quotaVisible = true;
    const button = {dataset: {entryId: '-1'}, disabled: false,
        remove() { this.removed = true; },
        closest: () => ({querySelector: key => key.includes('feedback') ? feedback
            : key === '.quota-progress' ? (quotaVisible ? {} : null) : content})};
    let posted;
    context.fetch = async (url, options) => {
        posted = {url, options};
        return {ok: true, json: async () => ({success: true, status: 'ok', html: 'new quota'})};
    };
    context.formatQuotaResetTimes = () => {};
    context.refreshAccountPoolTable = () => { throw new Error('Must not refresh the whole list'); };
    await context.refreshAccountPoolQuota(button);
    assert.equal(posted.url, '/admin/account-pool/-1/usage');
    assert.equal(posted.options.method, 'POST');
    assert.equal(content.innerHTML, 'new quota');
    assert.equal(feedback.textContent, '刚刚更新');
    context.fetch = async () => { throw new Error('网络失败'); };
    await context.refreshAccountPoolQuota(button);
    assert.equal(button.disabled, false);
    assert.equal(feedback.textContent, '网络失败');
    assert.equal(content.innerHTML, 'new quota');
    quotaVisible = false;
    context.fetch = async () => ({ok: true, json: async () => ({
        success: true, status: 'unavailable', html: '待授权', error: '缺少授权',
    })});
    await context.refreshAccountPoolQuota(button);
    assert.equal(button.removed, true);
    assert.equal(feedback.textContent, '缺少授权');
});
