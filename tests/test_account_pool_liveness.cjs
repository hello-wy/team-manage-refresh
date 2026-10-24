const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');

function runtime() {
    const cells = [], requests = [], toasts = [], refreshes = [];
    let refreshCallback = null;
    const progress = {};
    const allButton = {disabled: false, setAttribute(name, value) {this[name] = value;}};
    const allLabel = {textContent: '一键验活'};
    const context = vm.createContext({AbortSignal, URL, URLSearchParams, console,
        document: {addEventListener() {}, querySelector() {return null;},
            getElementById: id => ({accountPoolLivenessProgress: progress,
                accountPoolCheckAllLiveness: allButton, accountPoolCheckAllLabel: allLabel})[id], querySelectorAll(selector) {
            if (selector === '.account-pool-liveness[data-entry-id]') return cells;
            if (selector === '.account-pool-entry-liveness') return cells.map(cell => cell.button);
            return [];
        }},
        window: {location: {origin: 'http://localhost', search: '?page=3&per_page=1&seat_filter=standard&liveness_filter=unchecked'}},
        showToast: (...args) => toasts.push(args),
        fetch: (url, options) => new Promise(resolve => requests.push({url, options, resolve})),
    });
    vm.runInContext(fs.readFileSync(`${__dirname}/../app/static/js/account-pool.js`, 'utf8'), context);
    context.refreshAccountPoolTable = async options => {
        refreshes.push(options);
        await refreshCallback?.();
    };
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
    return {context, cells, requests, toasts, refreshes, addCell, respond, progress, allButton, allLabel,
        onRefresh(callback) { refreshCallback = callback; }};
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
    assert.equal(env.requests[0].url, '/admin/account-pool/1/refresh');
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
    assert.equal(env.refreshes.length, 1);
    assert.equal(env.refreshes[0].refreshQuotas, false);
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

test('successful refresh redraw keeps the completed liveness state instead of restoring checking', async () => {
    const env = runtime(), original = env.addCell('56');
    env.onRefresh(async () => {
        env.cells.length = 0;
        const replacement = env.addCell('56');
        env.context.initAccountPoolRowActions({refreshQuotas: false});
        assert.equal(replacement.badge.textContent, '正常');
        assert.equal(replacement.button.disabled, false);
    });
    const check = env.context.checkAccountPoolEntryLiveness(original.button);
    env.respond(0, {success: true, status: 'alive', message: '密码、2FA 与 OAuth 登录验证通过', checked_at: '2030-01-01T12:00:00+08:00'});
    await check;
    assert.equal(env.refreshes[0].refreshQuotas, false);
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

async function flush() { await new Promise(resolve => setImmediate(resolve)); }

test('batch checks selected accounts across pages with two concurrent logins and per-row progress', async () => {
    const env = runtime();
    const rows = ['1', '2', '3', '4'].map(env.addCell);
    const batch = env.context.checkAccountPoolBatchLiveness([1, 2, 3, 4, 5, 5]);
    assert.equal(env.requests.length, 2);
    assert.equal(rows[0].badge.textContent, '验活中');
    assert.equal(rows[1].badge.textContent, '验活中');
    assert.equal(rows[2].badge.textContent, '等待验活');
    assert.equal(rows[2].button.disabled, true);
    await env.context.checkAccountPoolEntryLiveness(rows[2].button);
    await env.context.checkAccountPoolBatchLiveness([3]);
    assert.equal(env.requests.length, 2);
    env.respond(0, {success: true, status: 'alive', message: '验证通过'});
    await flush();
    assert.equal(env.requests.length, 3);
    assert.equal(rows[2].badge.textContent, '验活中');
    env.respond(1, {success: true, status: 'invalid', message: '密码无效'});
    await flush();
    assert.equal(env.requests.length, 4);
    env.respond(2, {success: false, error: '服务不可用'}, false);
    await flush();
    assert.equal(env.requests.length, 5);
    assert.equal(env.requests[4].url, '/admin/account-pool/5/liveness');
    assert.equal(rows[2].badge.textContent, '异常');
    env.respond(3, {success: true, status: 'alive', message: '验证通过'});
    env.respond(4, {success: true, status: 'alive', message: '验证通过'});
    await batch;
    assert.equal(env.progress.textContent, '批量验活完成 5/5 · 正常 3 · 异常 2');
    assert.equal(env.toasts.length, 0);
    assert.ok(rows.every(cell => !cell.button.disabled));
    const retry = env.context.checkAccountPoolBatchLiveness([3]);
    assert.equal(env.requests.length, 6);
    env.respond(5, {success: true, status: 'alive', message: '验证通过'});
    await retry;
    assert.equal(rows[2].badge.textContent, '正常');
});

test('batch skips an active single check and queued state survives a table redraw', async () => {
    const env = runtime(), first = env.addCell('1');
    env.addCell('2'); env.addCell('3'); env.addCell('4');
    const single = env.context.checkAccountPoolEntryLiveness(first.button);
    const batch = env.context.checkAccountPoolBatchLiveness([1, 2, 3, 4]);
    assert.equal(env.requests.length, 3);
    env.cells.length = 0;
    const queued = env.addCell('4');
    env.context.initAccountPoolRowActions();
    assert.equal(queued.badge.textContent, '等待验活');
    assert.equal(queued.button.disabled, true);
    env.respond(1, {success: true, status: 'alive', message: '通过'});
    await flush();
    assert.equal(queued.badge.textContent, '验活中');
    env.respond(2, {success: false, code: 'checking', error: '该账号正在验活'}, false);
    env.respond(3, {success: true, status: 'alive', message: '通过'});
    await batch;
    assert.equal(env.progress.textContent, '批量验活完成 4/4 · 正常 2 · 异常 0 · 跳过 2（已在验活）');
    env.respond(0, {success: true, status: 'alive', message: '通过'});
    await single;
});

test('one-click liveness snapshots all filtered pages and includes owners without selecting rows', async () => {
    const env = runtime(), first = env.addCell('1');
    const all = env.context.checkAllAccountPoolLiveness();
    assert.equal(env.allButton.disabled, true);
    assert.equal(env.allLabel.textContent, '读取账号…');
    assert.equal(env.requests[0].url.pathname, '/admin/account-pool/liveness-targets');
    assert.equal(env.requests[0].url.searchParams.get('liveness_filter'), 'unchecked');
    assert.equal(env.requests[0].url.searchParams.get('seat_filter'), 'standard');
    await env.context.checkAllAccountPoolLiveness();
    await env.context.checkAccountPoolBatchLiveness([1]);
    assert.equal(env.requests.length, 1);
    env.respond(0, {success: true, ids: [1, 2, 3, -7], total: 4});
    await flush();
    assert.equal(env.requests.length, 3);
    assert.equal(first.badge.textContent, '验活中');
    assert.equal(env.allLabel.textContent, '验活中…');
    env.respond(1, {success: true, status: 'alive', message: '通过'});
    env.respond(2, {success: true, status: 'invalid', message: '密码错误'});
    await flush();
    assert.equal(env.requests.length, 5);
    assert.equal(env.requests[4].url, '/admin/account-pool/-7/liveness');
    env.respond(3, {success: true, status: 'alive', message: '通过'});
    env.respond(4, {success: true, status: 'alive', message: '通过'});
    await all;
    assert.equal(env.progress.textContent, '一键验活完成 4/4 · 正常 3 · 异常 1');
    assert.equal(env.allButton.disabled, false);
    assert.equal(env.allLabel.textContent, '一键验活');
});

test('one-click liveness handles empty lists and target read failures without starting logins', async () => {
    const env = runtime();
    const empty = env.context.checkAllAccountPoolLiveness();
    env.respond(0, {success: true, ids: [], total: 0});
    await empty;
    assert.equal(env.progress.textContent, '当前列表没有可验活的账号');
    assert.equal(env.allButton.disabled, false);
    const failure = env.context.checkAllAccountPoolLiveness();
    env.respond(1, {success: false, detail: '账号列表读取失败'}, false);
    await failure;
    assert.equal(env.requests.length, 2);
    assert.equal(env.progress.textContent, '账号列表读取失败');
    assert.equal(env.allButton.disabled, false);
    assert.equal(env.allButton['aria-busy'], 'false');
});
