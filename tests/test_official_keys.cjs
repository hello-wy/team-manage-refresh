const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');

const SOURCE = fs.readFileSync(`${__dirname}/../app/static/js/official-keys.js`, 'utf8')
    .replace(/^import .*;\n/m, '');
const EXPIRATION_SOURCE = fs.readFileSync(
    `${__dirname}/../app/static/js/official-key-expiration.js`, 'utf8'
).replace(/^export /gm, '');
const ENTRIES = [
    {id: 1, account: 'first@example.com', api_key: 'first-key-long-enough-for-truncation'},
    {id: 2, account: 'second@example.com', api_key: 'second-key'},
    {id: 3, account: 'third@example.com', api_key: 'third-key'},
];

function element(tag = 'div') {
    return {
        tag, dataset: {}, children: [], listeners: {}, hidden: false,
        addEventListener(name, handler) { this.listeners[name] = handler; },
        append(...children) { this.children.push(...children); },
        replaceChildren(...children) { this.children = children; },
        focus() {}, scrollIntoView() {}, reset() {},
    };
}

function response(status, data) {
    return {ok: status >= 200 && status < 300, status,
        headers: {get: () => 'application/json'}, json: async () => data};
}

function successfulBalances() {
    return Object.fromEntries(ENTRIES.map(entry => [entry.id, {
        status: 'ok', balance: {
            total_granted: '20', total_used: '3.25',
            total_available: '16.75', currency: 'USD',
        },
    }]));
}

async function runtime(handler, balances = successfulBalances()) {
    const ids = {}, requests = [], toasts = [], copied = [];
    const names = ['Form', 'Account', 'Value', 'Cancel', 'Save', 'Status', 'Rows', 'FormTitle',
        'ExportAll', 'ExportStatus', 'ExportErrors', 'Count', 'Refresh'];
    names.forEach(name => { ids[`officialKey${name}`] = element(); });
    const context = vm.createContext({
        document: {
            getElementById: id => ids[id], createElement: element,
            querySelectorAll: () => Object.values(ids),
        },
        fetch: async (url, options) => {
            requests.push({url, options});
            if (url.endsWith('/data')) return response(200, {
                keys: ENTRIES, exports: {}, expirations: Object.fromEntries(ENTRIES.map(entry => [
                    entry.id, {html: '<span>-</span>', can_edit: false, note: '测试到期信息'}
                ])), balances,
            });
            return handler(url, options, ids);
        },
        showToast: (...args) => toasts.push(args),
        confirmPageAction: async () => true, copyToClipboard: async value => copied.push(value),
    });
    vm.runInContext(`${EXPIRATION_SOURCE}\n${SOURCE}`, context);
    await new Promise(resolve => setImmediate(resolve));
    return {context, ids, requests, toasts, copied};
}

test('one-click export sends all records and shows individual failures without stopping', async () => {
    const sent = [];
    const env = await runtime(async (url, options, ids) => {
        sent.push(url);
        assert.equal(options.method, 'POST');
        assert.equal(ids.officialKeyExportAll.disabled, true);
        assert.equal(options.body, undefined);
        if (url.includes('/2/')) return response(400, {detail: '分组不存在'});
        return response(200, {success: true, export: {account_id: 1001}});
    });
    await env.ids.officialKeyExportAll.listeners.click();
    assert.deepEqual(sent, ENTRIES.map(entry => `/admin/official-keys/${entry.id}/sub2api`));
    assert.match(env.ids.officialKeyExportStatus.textContent, /成功 2 条，失败 1 条/);
    assert.equal(env.ids.officialKeyExportErrors.children.length, 1);
    assert.match(env.ids.officialKeyExportErrors.children[0].textContent, /second@example.com：分组不存在/);
    assert.equal(env.ids.officialKeyExportAll.disabled, false);
});

test('saving a key never automatically exports and preserves explicit API errors', async () => {
    const env = await runtime(async (url, options) => {
        assert.equal(url, '/admin/official-keys');
        assert.deepEqual(JSON.parse(options.body), {account: 'new@example.com', api_key: 'new-key'});
        return response(201, {success: true});
    });
    env.ids.officialKeyAccount.value = 'new@example.com';
    env.ids.officialKeyValue.value = 'new-key';
    await env.ids.officialKeyForm.listeners.submit({preventDefault() {}});
    assert.equal(env.requests.filter(request => request.options.method === 'POST').length, 1);
    assert.equal(env.requests.some(request => request.url.endsWith('/sub2api')), false);
    assert.deepEqual(env.toasts[0], ['官key 已保存', 'success']);
});

test('single export reports the target ID and refreshes statuses', async () => {
    const env = await runtime(async () => response(200, {export: {account_id: 1001}}));
    const row = env.ids.officialKeyRows.children[0];
    const exportButton = row.children[5].children[0].children[1];
    await exportButton.listeners.click();
    assert.equal(env.requests[1].url, '/admin/official-keys/1/sub2api');
    assert.deepEqual(env.toasts[0], ['已导出到 Sub2API（#1001）', 'success']);
    assert.equal(env.requests.at(-1).url, '/admin/official-keys/data');
});

test('balance column renders the server-provided available credit', async () => {
    const env = await runtime(async () => response(200, {}));
    const row = env.ids.officialKeyRows.children[0];
    assert.equal(row.children[3].children[0].textContent, '可用 16.75 USD');
});

test('balance column exposes per-key failures', async () => {
    const env = await runtime(async () => response(200, {}), {
        1: {status: 'error', error: '余额查询失败'},
        2: {status: 'error', error: '余额查询失败'},
        3: {status: 'error', error: '余额查询失败'},
    });
    assert.match(env.ids.officialKeyRows.children[0].children[3].children[0].textContent, /余额查询失败/);
});

test('non-JSON gateway failures remain visible for every affected record', async () => {
    const env = await runtime(async () => ({
        ok: false, status: 502, headers: {get: () => 'text/html'},
        json() { throw Error('must not parse HTML'); },
    }));
    await env.ids.officialKeyExportAll.listeners.click();
    assert.match(env.ids.officialKeyExportStatus.textContent, /成功 0 条，失败 3 条/);
    assert.match(env.ids.officialKeyExportErrors.children[0].textContent, /非 JSON.*HTTP 502/);
    assert.equal(env.ids.officialKeyExportAll.disabled, false);
});

test('display abbreviates the key while copy retains all content', async () => {
    const env = await runtime(async () => response(200, {}));
    const row = env.ids.officialKeyRows.children[0];
    assert.equal(row.children[1].innerHTML, '<span>-</span>');
    assert.equal(row.children[2].children[0].textContent, 'first-key-…tion');
    await row.children.at(-1).children[0].children[0].listeners.click();
    assert.deepEqual(env.copied, [ENTRIES[0].api_key]);
});
