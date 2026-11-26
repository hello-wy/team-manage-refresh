const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');

function runtime({confirmed = true, jobStatus = 'completed'} = {}) {
    const calls = [], toasts = [], listeners = {};
    const context = vm.createContext({
        window: {}, document: {addEventListener(name, fn) { listeners[name] = fn; }, querySelectorAll: () => []},
        confirmPageAction: async () => confirmed, confirmAccountPoolAction: async () => confirmed,
        showToast: (...args) => toasts.push(args), refreshAccountPoolTable: async () => {},
        setTimeout: fn => { queueMicrotask(fn); },
        fetch: async (url, options) => {
            calls.push({url, options});
            if (url.includes('cpa-export-jobs')) return {ok: true, json: async () => ({
                status: jobStatus, filename: 'codex.json', error: jobStatus === 'failed' ? 'CPA HTTP 401' : null,
            })};
            return {ok: true, json: async () => ({success: true, job_id: 9, data: {filename: 'codex.json'}})};
        },
    });
    vm.runInContext(readFileSync('app/static/js/cpa-export.js', 'utf8'), context);
    const button = {dataset: {entryId: '7', email: 'member@example.com', workspaceId: 'workspace-b'},
        disabled: false, setAttribute() {}, removeAttribute() {}};
    return {calls, toasts, context, button, click() { listeners.click({target: {closest: () => button}}); }};
}

const finishMicrotasks = () => new Promise(resolve => setImmediate(resolve));

test('Team export posts the selected row and reports the CPA filename', async () => {
    const env = runtime();
    await env.context.window.pushTeamToCpa('7', 'owner@example.com');
    assert.equal(env.calls[0].url, '/admin/teams/7/push-cpa');
    assert.equal(env.calls[0].options.method, 'POST');
    assert.equal(env.toasts.at(-1)[1], 'success');
});

test('CPA export cancellation sends no request for either entry point', async () => {
    const env = runtime({confirmed: false});
    await env.context.window.pushTeamToCpa('7', 'owner@example.com');
    env.click();
    await finishMicrotasks();
    assert.equal(env.calls.length, 0);
});

test('pool export preserves selected workspace and polls the CPA job endpoint', async () => {
    const env = runtime();
    env.click();
    await finishMicrotasks();
    assert.equal(env.calls[0].url, '/admin/account-pool/7/export-cpa');
    assert.deepEqual(JSON.parse(env.calls[0].options.body), {workspace_id: 'workspace-b'});
    assert.equal(env.calls[1].url, '/admin/account-pool/cpa-export-jobs/9');
    assert.equal(env.toasts.at(-1)[1], 'success');
    assert.equal(env.button.disabled, false);
});

test('failed CPA jobs expose their error without reporting upload success', async () => {
    const env = runtime({jobStatus: 'failed'});
    env.click();
    await finishMicrotasks();
    assert.deepEqual(env.toasts.at(-1), ['CPA HTTP 401', 'error']);
    assert.ok(!env.toasts.some(([message]) => message.includes('已导出到 CPA')));
    assert.equal(env.button.disabled, false);
});

test('account pool batch button submits the selection and displays mixed results', async () => {
    const env = runtime();
    const buttons = new Map();
    const listeners = {};
    const panel = {hidden: true};
    env.context.document.addEventListener = (name, callback) => { listeners[name] = callback; };
    env.context.document.getElementById = id => {
        if (id === 'accountPoolBatchCpaProgress') return panel;
        if (id === 'accountPoolBatchLiveness') return null;
        if (!buttons.has(id)) {
            const attributes = new Set();
            buttons.set(id, {addEventListener(name, callback) { this[name] = callback; },
                hasAttribute: name => attributes.has(name), setAttribute: name => attributes.add(name),
                removeAttribute: name => attributes.delete(name)});
        }
        return buttons.get(id);
    };
    env.context.sessionStorage = {getItem: () => null};
    env.context.fetch = async (url, options) => {
        env.calls.push({url, options});
        if (url.endsWith('/batch/cpa')) return {ok: true, json: async () => ({jobs: [
            {id: 9, email: 'first@example.com'}, {id: 10, email: 'second@example.com'},
        ]})};
        return {ok: true, json: async () => url.endsWith('/9') ? {status: 'completed'} :
            {status: 'failed', error: 'CPA HTTP 401', workspace_id: 'workspace-b'}};
    };
    vm.runInContext(readFileSync('app/static/js/account-pool-batch.js', 'utf8'), env.context);
    listeners.DOMContentLoaded();
    const button = buttons.get('accountPoolBatchCpa');
    assert.equal(button.disabled, true);
    vm.runInContext('accountPoolSelection.add(7).add(11); updateAccountPoolSelectionUi();', env.context);
    assert.equal(button.disabled, false);
    await button.click({currentTarget: button});
    assert.equal(env.calls[0].url, '/admin/account-pool/batch/cpa');
    assert.deepEqual(JSON.parse(env.calls[0].options.body), {ids: [7, 11]});
    assert.equal(panel.hidden, false);
    assert.match(panel.textContent, /成功 1，失败 1，进行中 0/);
    assert.match(panel.textContent, /second@example.com（workspace-b）：CPA HTTP 401/);
    assert.equal(button.disabled, false);
});
