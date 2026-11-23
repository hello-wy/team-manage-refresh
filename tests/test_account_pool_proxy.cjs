const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');
const source = fs.readFileSync(`${__dirname}/../app/static/js/account-pool-proxy.js`, 'utf8');
const response = (data, ok = true) => ({ok, json: async () => data});
const data = () => ({success: true, proxies: [
    {id: 1, name: '美国出口', address: 'socks5h://host:1080', enabled: true},
    {id: 2, name: '停用出口', address: 'http://host:80', enabled: false},
], default: {mode: 'proxy', proxy_id: 1}, legacy: {enabled: false, address: ''},
account: {email: 'user@example.com', mode: 'inherit', proxy_id: null}});

function runtime(fetcher = async () => response(data())) {
    const elements = new Map(), documentEvents = {}, requests = [], toasts = [];
    const make = id => {
        const node = {id, value: '', textContent: '', hidden: false, disabled: false,
            dataset: {}, events: {}, children: [], focus() {node.focused = true;},
            addEventListener(name, callback) {node.events[name] = callback;},
            setAttribute(name, value) {node[name] = value;},
            closest() {return null;},
            replaceChildren() {node.children = []; node.value = '';},
            appendChild(option) {node.children.push(option);},
        };
        elements.set(id, node); return node;
    };
    for (const id of ['accountPoolProxyDialog', 'accountPoolProxyForm', 'accountPoolProxySelect',
        'accountPoolProxySave', 'accountPoolProxyError', 'accountPoolProxyPreview',
        'accountPoolProxyTitle', 'accountPoolProxyEmail', 'accountPoolProxyDescription']) make(id);
    const cancel = make('cancel'), dialog = elements.get('accountPoolProxyDialog');
    dialog.open = false;
    dialog.showModal = () => {dialog.open = true;};
    dialog.close = () => {dialog.open = false; dialog.events.close?.();};
    dialog.querySelectorAll = () => [cancel];
    const button = make('accountProxy');
    button.dataset = {entryId: '17', email: 'user@example.com'};
    const defaultButton = make('accountPoolDefaultProxy');
    const context = vm.createContext({
        document: {getElementById: id => elements.get(id), createElement: () => ({}),
            addEventListener(name, callback) {documentEvents[name] = callback;},
            querySelectorAll: () => [button]},
        window: {showToast: (...args) => toasts.push(args),
            refreshAccountPoolTable: async () => {context.refreshed = true;}},
        fetch: async (...args) => {requests.push(args); return fetcher(...args);},
        AbortSignal, encodeURIComponent, console,
    });
    vm.runInContext(source, context);
    const click = target => documentEvents.click({target: {closest: () => target}});
    const settle = () => new Promise(resolve => setImmediate(resolve));
    const submit = () => elements.get('accountPoolProxyForm').events.submit({preventDefault() {}});
    return {context, elements, requests, toasts, dialog, button, defaultButton, cancel, click, settle, submit};
}

test('account proxy dialog loads only its account and names disabled nodes', async () => {
    const state = runtime(); state.click(state.button); await state.settle();
    assert.equal(state.requests[0][0], '/admin/account-pool/proxy-options?entry_id=17');
    assert.equal(state.elements.get('accountPoolProxyTitle').textContent, '设置账号代理');
    assert.match(state.elements.get('accountPoolProxyPreview').textContent, /美国出口/);
    const options = state.elements.get('accountPoolProxySelect').children;
    assert.equal(options[3].disabled, true);
    assert.match(options[3].textContent, /停用出口/);
    assert.equal(state.elements.get('accountPoolProxySave').disabled, true);
});

test('saving a changed account proxy refreshes the list and returns focus', async () => {
    const state = runtime(async (url, options) => response(options.method ? {success: true} : data()));
    state.click(state.button); await state.settle();
    const select = state.elements.get('accountPoolProxySelect');
    select.value = 'proxy:1'; select.events.change();
    assert.equal(state.elements.get('accountPoolProxySave').disabled, false);
    await state.submit();
    assert.equal(state.requests[1][0], '/admin/account-pool/17/proxy');
    assert.deepEqual(JSON.parse(state.requests[1][1].body), {mode: 'proxy', proxy_id: 1});
    assert.equal(state.context.refreshed, true);
    assert.equal(state.dialog.open, false);
    assert.equal(state.button.focused, true);
});

test('default proxy is edited from the pool without selecting an account', async () => {
    const state = runtime(async (url, options) => response(options.method ? {success: true} : data()));
    state.click(state.defaultButton); await state.settle();
    assert.equal(state.requests[0][0], '/admin/account-pool/proxy-options');
    const select = state.elements.get('accountPoolProxySelect');
    assert.equal(select.children[0].textContent, '沿用系统代理设置');
    select.value = 'direct'; select.events.change(); await state.submit();
    assert.equal(state.requests[1][0], '/admin/account-pool/proxy-default');
    assert.deepEqual(JSON.parse(state.requests[1][1].body), {mode: 'direct', proxy_id: null});
});

test('failed save preserves the selection for correction or retry', async () => {
    const state = runtime(async (url, options) => options.method
        ? response({success: false, detail: '请选择已启用的代理'}, false) : response(data()));
    state.click(state.button); await state.settle();
    const select = state.elements.get('accountPoolProxySelect');
    select.value = 'proxy:1'; select.events.change(); await state.submit();
    assert.equal(state.dialog.open, true);
    assert.equal(select.value, 'proxy:1');
    assert.match(state.elements.get('accountPoolProxyError').textContent, /已启用/);
    assert.equal(state.elements.get('accountPoolProxySave').disabled, false);
});

test('a closed dialog ignores its late load response', async () => {
    let resolve;
    const state = runtime(() => new Promise(done => {resolve = done;}));
    state.click(state.button); state.cancel.events.click();
    resolve(response(data())); await state.settle();
    assert.equal(state.dialog.open, false);
    assert.equal(state.elements.get('accountPoolProxySelect').children.length, 1);
    assert.equal(state.elements.get('accountPoolProxySave').disabled, true);
});

test('saving blocks duplicate submission and closing until completion', async () => {
    let resolve;
    const state = runtime(async (url, options) => options.method
        ? new Promise(done => {resolve = done;}) : response(data()));
    state.click(state.button); await state.settle();
    const select = state.elements.get('accountPoolProxySelect');
    select.value = 'direct'; select.events.change();
    const pending = state.submit(); await state.settle();
    let prevented = false;
    state.dialog.events.cancel({preventDefault() {prevented = true;}});
    state.cancel.events.click(); await state.submit();
    assert.equal(prevented, true);
    assert.equal(state.dialog.open, true);
    assert.equal(state.requests.length, 2);
    resolve(response({success: true})); await pending;
    assert.equal(state.dialog.open, false);
});
