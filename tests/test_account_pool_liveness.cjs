const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');

function runtime() {
    const cells = [], requests = [], toasts = [];
    const context = vm.createContext({AbortSignal, console,
        document: {addEventListener() {}, querySelectorAll(selector) {
            if (selector === '.account-pool-liveness[data-entry-id]') return cells;
            if (selector === '.account-pool-entry-liveness') return cells.map(cell => cell.button);
            return [];
        }},
        window: {}, showToast: (...args) => toasts.push(args),
        fetch: (url, options) => new Promise(resolve => requests.push({url, options, resolve})),
    });
    vm.runInContext(fs.readFileSync(`${__dirname}/../app/static/js/account-pool.js`, 'utf8'), context);
    context.refreshAccountPoolTable = () => {throw Error('Must update only the liveness column');};
    function addCell(id) {
        const badge = {textContent: '未检测'}, message = {}, time = {}, detail = {}, checked = {};
        const button = {disabled: false, dataset: {entryId: id, email: `${id}@example.com`},
            addEventListener() {}, closest: () => cell};
        const cell = {button, badge, message, time, detail, checked, dataset: {entryId: id},
            setAttribute(name, value) {this[name] = value;},
            closest: () => ({querySelector: key => key === '[data-liveness-message]' ? detail : checked}),
            querySelector: key => ({'.status-badge': badge, '.account-pool-liveness-message': message,
                '.account-pool-liveness-time': time, '.account-pool-entry-liveness': button})[key]};
        cells.push(cell);
        return cell;
    }
    function respond(index, payload, ok = true) {
        requests[index].resolve({ok, json: async () => payload});
    }
    return {context, cells, requests, toasts, addCell, respond};
}

test('click immediately shows checking in the chosen column and prevents duplicate checks', async () => {
    const env = runtime(), first = env.addCell('1'), other = env.addCell('2');
    const check = env.context.checkAccountPoolEntryLiveness(first.button);
    assert.equal(first.badge.textContent, '验活中');
    assert.equal(first['aria-busy'], 'true');
    assert.equal(first.button.disabled, true);
    assert.equal(other.badge.textContent, '未检测');
    await env.context.checkAccountPoolEntryLiveness(first.button);
    assert.equal(env.requests.length, 1);
    assert.equal(env.requests[0].url, '/admin/account-pool/1/liveness');
    assert.equal(env.requests[0].options.method, 'POST');
    assert.equal(env.requests[0].options.cache, 'no-store');
    assert.ok(env.requests[0].options.signal instanceof AbortSignal);
    env.respond(0, {success: true, status: 'alive', message: '密码、2FA 登录验证通过', checked_at: '2030-01-01T12:00:00+08:00'});
    await check;
    assert.equal(first.badge.textContent, '正常');
    assert.equal(first['aria-busy'], 'false');
    assert.equal(first.button.disabled, false);
    assert.equal(first.time.hidden, false);
    assert.equal(first.detail.textContent, '密码、2FA 登录验证通过');
    assert.equal(other.badge.textContent, '未检测');
});

test('failure is visible in the column with its reason and can be retried', async () => {
    const env = runtime(), cell = env.addCell('-1');
    const check = env.context.checkAccountPoolEntryLiveness(cell.button);
    env.respond(0, {success: true, status: 'invalid', message: '<Token 已失效>', checked_at: '2030-01-01T12:00:00+08:00'});
    await check;
    assert.equal(cell.badge.textContent, '异常');
    assert.equal(cell.message.textContent, '<Token 已失效>');
    assert.match(cell.badge.className, /status-error/);
    assert.equal(cell.button.disabled, false);
    const retry = env.context.checkAccountPoolEntryLiveness(cell.button);
    assert.equal(cell.badge.textContent, '验活中');
    env.respond(1, {success: true, status: 'alive', message: 'Token 验证通过'});
    await retry;
    assert.equal(cell.badge.textContent, '正常');
});

test('partial list redraw retains in-flight state and completion updates the replacement row', async () => {
    const env = runtime(), original = env.addCell('1');
    const check = env.context.checkAccountPoolEntryLiveness(original.button);
    env.cells.length = 0;
    const replacement = env.addCell('1');
    env.context.initAccountPoolRowActions();
    assert.equal(replacement.badge.textContent, '验活中');
    assert.equal(replacement.button.disabled, true);
    env.respond(0, {success: true, status: 'alive', message: '验证通过'});
    await check;
    assert.equal(replacement.badge.textContent, '正常');
    assert.equal(replacement.button.disabled, false);
});

test('network failure and server conflict show feedback and restore the retry button', async () => {
    const env = runtime(), cell = env.addCell('1');
    env.context.fetch = async () => {throw new Error('网络连接失败');};
    await env.context.checkAccountPoolEntryLiveness(cell.button);
    assert.equal(cell.badge.textContent, '异常');
    assert.equal(cell.message.textContent, '网络连接失败');
    assert.equal(cell.button.disabled, false);
    env.context.fetch = async () => ({ok: false, json: async () => ({success: false, code: 'checking', error: '该账号正在验活'})});
    await env.context.checkAccountPoolEntryLiveness(cell.button);
    assert.equal(cell.button.disabled, false);
    assert.equal(env.toasts.at(-1)[0], '该账号正在验活');
});
