const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');

function button(id) {
    const content = {innerHTML: 'cached quota'};
    const feedback = {textContent: ''};
    return {
        dataset: {entryId: String(id), workspaceId: 'team-a'}, isConnected: true, disabled: false,
        content, feedback,
        addEventListener(event, handler) { this[event] = handler; },
        closest: () => ({querySelector: key => key.includes('feedback') ? feedback : content}),
    };
}

function runtime(buttons) {
    const context = vm.createContext({
        document: {querySelectorAll: () => buttons}, window: {},
        formatQuotaResetTimes() {},
    });
    vm.runInContext(fs.readFileSync(`${__dirname}/../app/static/js/account-pool-quota.js`, 'utf8'), context);
    return context;
}

function result(id, overrides = {}) {
    return {entry_id: id, workspace_id: 'team-a', status: 'ok', html: `quota-${id}`, ...overrides};
}

test('loading the visible table fetches its accounts in one batch and binds retries once', async () => {
    const buttons = [button(1), button(-2)];
    const context = runtime(buttons);
    const calls = [];
    context.fetch = async (url, options) => {
        calls.push({url, options});
        assert.ok(buttons.every(item => item.disabled));
        return {ok: true, json: async () => ({success: true, entries: [result(1), result(-2)]})};
    };
    await context.initAccountPoolQuota();
    assert.equal(calls[0].url, '/admin/account-pool/usage');
    assert.deepEqual(JSON.parse(calls[0].options.body), {entry_ids: [1, -2]});
    assert.equal(buttons[0].content.innerHTML, 'quota-1');
    assert.equal(buttons[1].feedback.textContent, '刚刚更新');
    assert.ok(buttons.every(item => !item.disabled && item.click));
    await context.initAccountPoolQuota();
    assert.equal(calls.length, 1);
});

test('quota failures remain visible and can be retried for only that account', async () => {
    const target = button(1);
    const context = runtime([target]);
    context.fetch = async () => ({ok: true, json: async () => ({success: true, entries: [
        result(1, {status: 'invalid', error: '登录已失效', html: 'invalid quota'}),
    ]})});
    await context.initAccountPoolQuota();
    assert.equal(target.feedback.textContent, '登录已失效');
    assert.equal(target.disabled, false);
    const calls = [];
    context.fetch = async (url) => {
        calls.push(url);
        return {ok: true, json: async () => ({success: true, ...result(1)})};
    };
    await target.click();
    assert.deepEqual(calls, ['/admin/account-pool/1/usage']);
    assert.equal(target.feedback.textContent, '刚刚更新');
    assert.equal(target.content.innerHTML, 'quota-1');
});

test('late batches do not overwrite a replaced row or a different workspace', async () => {
    const detached = button(1), changed = button(2);
    const context = runtime([detached, changed]);
    let finish;
    context.fetch = () => new Promise(resolve => { finish = resolve; });
    const pending = context.initAccountPoolQuota();
    detached.isConnected = false;
    changed.dataset.workspaceId = 'team-b';
    finish({ok: true, json: async () => ({success: true, entries: [result(1), result(2)]})});
    await pending;
    assert.equal(detached.content.innerHTML, 'cached quota');
    assert.equal(changed.content.innerHTML, 'cached quota');
    assert.equal(changed.feedback.textContent, '账号的 Team 已变化，请刷新列表');
});

test('network and incomplete response errors preserve quota and leave the refresh control usable', async () => {
    const target = button(1);
    const context = runtime([target]);
    context.fetch = async () => { throw new Error('网络失败'); };
    await context.initAccountPoolQuota();
    assert.equal(target.feedback.textContent, '网络失败');
    assert.equal(target.content.innerHTML, 'cached quota');
    assert.equal(target.disabled, false);
    context.fetch = async () => ({ok: true, json: async () => ({success: true, entries: []})});
    await context.refreshVisibleAccountPoolQuotas([target]);
    assert.equal(target.feedback.textContent, '未返回该账号的额度，请重试');
    assert.equal(target.disabled, false);
});
