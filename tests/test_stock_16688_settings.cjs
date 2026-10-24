const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function harness(result) {
    const inputs = Object.fromEntries(Object.entries({stock16688Gateway: 'https://www.16688.com.cn',
        stock16688AppId: 'my-app', stock16688GoodsNo: 'G10001', stock16688Secret: 'private-secret'})
        .map(([key, value]) => [key, {value}]));
    const button = {disabled: false}, calls = [], messages = [];
    let submit;
    const form = {querySelector: () => button, addEventListener: (name, fn) => { submit = fn; }};
    const context = vm.createContext({document: {
        getElementById: id => id === 'stock16688Form' ? form : inputs[id],
        addEventListener: (name, fn) => fn(),
    }, fetch: async (url, options) => {
        calls.push({url, options});
        return {ok: result.success, json: async () => result};
    }, showToast: (...args) => messages.push(args)});
    vm.runInContext(fs.readFileSync('app/static/js/stock-16688-settings.js', 'utf8'), context);
    return {inputs, button, calls, messages, submit: () => submit({preventDefault() {}})};
}

test('16688 settings save sends correct fields and clears the secret after success', async () => {
    const env = harness({success: true, gateway: 'https://www.16688.com.cn/openApi', message: '已保存'});
    await env.submit();
    assert.equal(env.calls[0].url, '/admin/settings/16688');
    const body = JSON.parse(env.calls[0].options.body);
    assert.equal(body.app_id, 'my-app');
    assert.equal(body.goods_no, 'G10001');
    assert.equal(body.secret, 'private-secret');
    assert.equal(env.inputs.stock16688Secret.value, '');
    assert.match(env.inputs.stock16688Secret.placeholder, /已配置/);
    assert.equal(env.inputs.stock16688Gateway.value, 'https://www.16688.com.cn/openApi');
    assert.equal(env.button.disabled, false);
});

test('failed settings save keeps user input available for correction', async () => {
    const env = harness({success: false, error: '请填写商品编号'});
    await env.submit();
    assert.equal(env.inputs.stock16688Secret.value, 'private-secret');
    assert.equal(env.button.disabled, false);
    assert.deepEqual(env.messages[0], ['请填写商品编号', 'error']);
});
