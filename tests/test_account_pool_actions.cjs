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
    for (const name of ['page-confirm.js', 'account-pool-confirm.js', 'account-pool.js']) {
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

test('exit and replacement countdowns reach waiting states and respect timezone', () => {
    const {context} = runtime();
    const now = Date.parse('2030-01-01T00:00:00Z');
    for (const kind of ['replacement', 'exit']) {
        const label = value => context.accountPoolCountdownLabel(value, now, kind);
        assert.equal(label('2030-01-01T09:02:03+08:00'), `${kind === 'exit' ? '下线倒计时' : '预计上线倒计时'} 01:02:03`);
        assert.equal(label('2030-01-01T08:00:00+08:00'), kind === 'exit' ? '等待下线执行' : '等待补位执行');
        assert.equal(label('2029-12-31T08:00:00+08:00'), kind === 'exit' ? '等待下线执行' : '等待补位执行');
        assert.equal(label(''), kind === 'exit' ? '下线时间待确认' : '上线时间待确认');
    }
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

test('single account liveness posts only the chosen owner and restores retry after failure', async () => {
    const {context} = runtime();
    const calls = [], toasts = [];
    context.showToast = (...args) => toasts.push(args);
    context.refreshAccountPoolTable = async () => calls.push('refresh');
    context.fetch = async (url, options) => {
        calls.push([url, options.method]);
        return {ok: true, json: async () => ({success: true, status: 'alive', message: 'Token verified'})};
    };
    const button = {disabled: false, dataset: {entryId: '-2', email: 'owner@example.com'}};
    await context.checkAccountPoolEntryLiveness(button);
    assert.deepEqual(calls, [['/admin/account-pool/-2/liveness', 'POST'], 'refresh']);
    assert.equal(button.disabled, false);
    context.fetch = async () => {throw new Error('network unavailable');};
    await context.checkAccountPoolEntryLiveness(button);
    assert.equal(button.disabled, false);
    assert.equal(toasts.at(-1)[0], 'network unavailable');
});

test('workbench member removal waits for the page dialog and cancellation sends no request', async () => {
    const env = runtime();
    const main = fs.readFileSync(`${__dirname}/../app/static/js/main.js`, 'utf8');
    vm.runInContext(main.slice(main.indexOf('async function deleteMember('), main.indexOf('async function rotateMember(')), env.context);
    let calls = 0;
    env.context.apiCall = async () => {calls++; return {success: true};};
    env.context.showToast = () => {};
    env.context.loadModalMemberList = async () => {};
    env.context.window.currentTeamId = 1;
    let removal = env.context.deleteMember(1, 'user', 'member@example.com', true);
    assert.equal(calls, 0);
    env.dialog().controls['[data-cancel]'].click();
    await removal;
    assert.equal(calls, 0);
    removal = env.context.deleteMember(1, 'user', 'member@example.com', true);
    env.dialog().controls['[data-confirm]'].click();
    await removal;
    assert.equal(calls, 1);
});
