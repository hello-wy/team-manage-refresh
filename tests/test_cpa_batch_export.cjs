const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');

const template = readFileSync('app/templates/admin/index.html', 'utf8');
function section(start, end) {
    return template.slice(template.indexOf(start), template.indexOf(end));
}

function runtime({selected = [2, 5], confirmed = true, result} = {}) {
    const calls = [], toasts = [], buttons = new Map();
    let confirmations = 0, refreshes = 0;
    const response = result || {success: true, success_count: 1, failed_count: 1,
        message: '批量导出到 CPA：成功 1，失败 1',
        results: [{team_id: 2, success: true}, {team_id: 5, success: false, error: 'CPA HTTP 401'}]};
    const context = vm.createContext({
        window: {}, console,
        document: {
            addEventListener() {}, querySelectorAll: () => selected.map(value => ({value: String(value)})),
            getElementById(id) {
                if (!buttons.has(id)) buttons.set(id, {disabled: false});
                return buttons.get(id);
            },
        },
        confirmPageAction: async () => { confirmations++; return confirmed; },
        refreshAdminTeamTable: async () => { refreshes++; },
        showToast: (...args) => toasts.push(args),
        fetch: async (url, options) => { calls.push({url, options}); return {
            ok: response.success, json: async () => response,
        }; },
    });
    vm.runInContext(section('    function getSelectedTeamIds()', '    let batchRefreshProgressHideTimer'), context);
    vm.runInContext(section('    async function handleBatchAction(', '    async function runBatchRefreshTask('), context);
    vm.runInContext(readFileSync('app/static/js/cpa-export.js', 'utf8'), context);
    return {context, calls, toasts, buttons, get confirmations() { return confirmations; },
        get refreshes() { return refreshes; }};
}

test('batch CPA sends only checked Teams and exposes partial failure reasons', async () => {
    const env = runtime();
    await env.context.window.batchPushTeamsToCpa();
    assert.equal(env.calls[0].url, '/admin/teams/batch-push-cpa');
    assert.deepEqual(JSON.parse(env.calls[0].options.body), {ids: [2, 5]});
    assert.equal(env.toasts.at(-1)[1], 'warning');
    assert.match(env.toasts.at(-1)[0], /Team 5: CPA HTTP 401/);
    assert.equal(env.refreshes, 1);
    assert.equal(env.buttons.get('btnBatchPushCpa').disabled, false);
});

test('batch CPA cancel and empty selection send no requests', async () => {
    for (const options of [{confirmed: false}, {selected: []}]) {
        const env = runtime(options);
        await env.context.window.batchPushTeamsToCpa();
        assert.equal(env.calls.length, 0);
    }
});

test('all failed exports display the server error and restore controls', async () => {
    const env = runtime({result: {success: false, error: 'Team 2: 密码缺失；Team 5: CPA HTTP 401'}});
    await env.context.window.batchPushTeamsToCpa();
    assert.deepEqual(env.toasts.at(-1), ['Team 2: 密码缺失；Team 5: CPA HTTP 401', 'error']);
    assert.equal(env.refreshes, 0);
    assert.equal(env.buttons.get('btnBatchPushCpa').disabled, false);
});

test('CPA batch control is disabled while exporting and restored after network errors', async () => {
    const env = runtime();
    let reject, announceStarted;
    const started = new Promise(resolve => { announceStarted = resolve; });
    env.context.fetch = () => new Promise((resolve, fail) => { reject = fail; announceStarted(); });
    env.context.console = {error() {}};
    const pending = env.context.window.batchPushTeamsToCpa();
    await started;
    assert.equal(env.buttons.get('btnBatchPushCpa').disabled, true);
    reject(new Error('connection lost'));
    await pending;
    assert.equal(env.buttons.get('btnBatchPushCpa').disabled, false);
    assert.equal(env.toasts.at(-1)[1], 'error');
});

test('workbench exposes batch CPA with its logo and removes the pool transfer control', () => {
    assert.match(template, /id="btnBatchPushCpa"/);
    assert.match(template, /btnBatchPushCpa[^>]*>[\s\S]*?cpa-icon.jpg[\s\S]*?批量导出到 CPA/);
    assert.doesNotMatch(template, /btnBatchTransferPool|handleBatchTransferPool|转入福利车位/);
});
