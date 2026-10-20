const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

function harness({confirm = true, response = {success: true, message: '已重新登录并更新登录状态'}} = {}) {
    const source = readFileSync(join(__dirname, '../app/templates/admin/index.html'), 'utf8');
    const start = source.indexOf('    async function refreshTeam(');
    const end = source.indexOf('    async function refreshATInModal()', start);
    const calls = [], toasts = [], prompts = [];
    let tableRefreshes = 0;
    const context = vm.createContext({
        confirmPageAction: async message => { prompts.push(message); return confirm; },
        showToast: (...args) => toasts.push(args),
        fetch: async (url, options) => {
            calls.push({url, options});
            return {ok: response.success, json: async () => response};
        },
        refreshAdminTeamTable: async () => { tableRefreshes += 1; },
    });
    vm.runInContext(source.slice(start, end), context);
    return {context, calls, toasts, prompts, tableRefreshes: () => tableRefreshes};
}

test('refresh explains automatic recovery and shows the server result', async () => {
    const env = harness();
    const button = {disabled: false};
    await env.context.refreshTeam(1, button);
    assert.match(env.prompts[0], /密码和 2FA/);
    assert.equal(env.calls[0].url, '/api/teams/1/refresh');
    assert.equal(env.toasts.at(-1)[0], '已重新登录并更新登录状态');
    assert.equal(env.tableRefreshes(), 1);
    assert.equal(button.disabled, false);
});

test('cancel restores the refresh button without sending a request', async () => {
    const env = harness({confirm: false});
    const button = {disabled: false};
    await env.context.refreshTeam(1, button);
    assert.equal(env.calls.length, 0);
    assert.equal(button.disabled, false);
});

test('relogin failure remains visible and refreshes the status table', async () => {
    const env = harness({response: {success: false, error: '异常账号重新登录失败：密码错误'}});
    await env.context.refreshTeam(1, {disabled: false});
    assert.equal(env.toasts.at(-1)[0], '异常账号重新登录失败：密码错误');
    assert.equal(env.toasts.at(-1)[1], 'error');
    assert.equal(env.tableRefreshes(), 1);
});

test('repeated clicks do not issue a second login request', async () => {
    const env = harness();
    let finish;
    env.context.fetch = () => new Promise(resolve => { finish = resolve; });
    const button = {disabled: false};
    const pending = env.context.refreshTeam(1, button);
    await Promise.resolve();
    await env.context.refreshTeam(1, button);
    assert.equal(env.prompts.length, 1);
    finish({ok: true, json: async () => ({success: true, message: '完成'})});
    await pending;
    assert.equal(button.disabled, false);
});

test('network error restores button state', async () => {
    const env = harness();
    env.context.fetch = async () => { throw new Error('connection reset'); };
    const button = {disabled: false};
    await env.context.refreshTeam(1, button);
    assert.equal(env.toasts.at(-1)[1], 'error');
    assert.equal(button.disabled, false);
});
