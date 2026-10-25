const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');

function harness() {
    const preview = {className: '', innerHTML: '', textContent: ''};
    const count = {value: '1'}, warranty = {checked: false};
    const requests = [];
    const context = vm.createContext({
        document: {
            addEventListener() {},
            getElementById: id => ({
                codeGenerationCapacityPreview: preview,
                batchGenerate: {style: {display: 'block'}},
            })[id],
            querySelector: selector => selector.includes('count') ? count : warranty,
        },
        window: {}, console,
    });
    vm.runInContext(readFileSync('app/static/js/main.js', 'utf8'), context);
    context.apiCall = url => new Promise(resolve => requests.push({url, resolve}));
    return {context, preview, count, warranty, requests};
}

const capacity = {
    success: true, capacity_known: true, available: 2, reserved_codes: 1,
    workspaces: [{workspace_name: '<Team>', workspace_id: 'space-1', team_id: 7, standard_remaining: 2}],
};

test('known insufficient capacity keeps remaining seats, workspace identity and escaped names visible', () => {
    const {context, preview} = harness();
    context.renderCodeGenerationCapacityPreview({...capacity, success: false}, 3);
    assert.match(preview.innerHTML, /可用标准席位：2，本次需要：3/);
    assert.match(preview.innerHTML, /space-1/);
    assert.match(preview.innerHTML, /&lt;Team&gt;/);
    assert.match(preview.innerHTML, /标准席位不足/);
    assert.equal(preview.className, 'alert alert-warning');
});

test('changing quantity reuses loaded capacity without making upstream requests', async () => {
    const {context, preview, count, requests} = harness();
    const pending = context.refreshCodeGenerationCapacityPreview();
    count.value = '3';
    await context.refreshCodeGenerationCapacityPreview(false);
    assert.equal(requests.length, 1);
    requests[0].resolve({success: true, data: capacity});
    await pending;
    assert.match(preview.innerHTML, /本次需要：3/);
    count.value = '1';
    await context.refreshCodeGenerationCapacityPreview(false);
    assert.equal(requests.length, 1);
    assert.match(preview.innerHTML, /本次需要：1/);
    assert.equal(preview.className, 'alert alert-info');
});

test('switching warranty type ignores a late response for the previous inventory', async () => {
    const {context, preview, warranty, requests} = harness();
    const first = context.refreshCodeGenerationCapacityPreview();
    warranty.checked = true;
    const second = context.refreshCodeGenerationCapacityPreview();
    assert.match(requests[1].url, /has_warranty=true/);
    requests[1].resolve({success: true, data: {...capacity, available: 4}});
    await second;
    requests[0].resolve({success: true, data: capacity});
    await first;
    assert.match(preview.innerHTML, /可用标准席位：4/);
});

test('unknown inventory exposes a retry and generated snapshot identifies candidate workspaces', () => {
    const {context, preview} = harness();
    context.renderCodeGenerationCapacityPreview({success: false, error: '<unavailable>'});
    assert.match(preview.innerHTML, /&lt;unavailable&gt;/);
    assert.match(preview.innerHTML, /重试/);
    const snapshot = {innerHTML: ''};
    context.renderGeneratedCodeCapacitySnapshot(snapshot, capacity, 1);
    assert.match(snapshot.innerHTML, /space-1/);
    assert.match(snapshot.innerHTML, /Team ID/);
    assert.match(snapshot.innerHTML, /生成时不固定绑定/);
});
