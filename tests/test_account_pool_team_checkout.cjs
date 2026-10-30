const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');

const URL_VALUE = 'https://checkout.stripe.com/c/pay/cs_test_team';
const response = (status, data) => ({status, ok: status === 200, text: async () => JSON.stringify(data)});

function runtime() {
    const feedback = {}, fieldset = {}, textarea = {}, link = {};
    const result = {hidden: true, querySelector: key => key === 'textarea' ? textarea : link};
    const selectors = {'fieldset': fieldset, '[data-feedback]': feedback,
        '[data-result]': result, '[data-result] textarea': textarea};
    const context = vm.createContext({URL, console, document: {addEventListener() {}},
        window: {open() { throw Error('Must never open a new tab'); }},
        navigator: {clipboard: {writeText: async () => {}}}});
    vm.runInContext(fs.readFileSync(`${__dirname}/../app/static/js/account-pool-team-checkout.js`, 'utf8'), context);
    const options = {button: {}, dialog: {querySelector: key => selectors[key],
        close() { throw Error('Must keep the result dialog open'); }},
    form: {elements: {coupon: {value: ' PROMO '}}},
    account: {entryId: '7', email: 'member@example.com', workspaceId: 'personal'}};
    return {context, options, feedback, fieldset, result, textarea, link};
}

test('shows full checkout URL below coupon without opening a tab or closing dialog', async () => {
    const env = runtime();
    env.context.fetch = async (url, request) => {
        assert.equal(env.options.button.disabled, true);
        assert.match(env.feedback.textContent, /正在/);
        assert.equal(url, '/admin/account-pool/7/team-checkout');
        assert.deepEqual(JSON.parse(request.body), {workspace_id: 'personal', coupon: 'PROMO'});
        return response(200, {success: true, url: URL_VALUE});
    };
    await env.context.submitAccountPoolTeamCheckout(env.options);
    assert.equal(env.textarea.value, URL_VALUE);
    assert.equal(env.link.href, URL_VALUE);
    assert.equal(env.result.hidden, false);
    assert.equal(env.options.button.disabled, false);
    assert.match(env.feedback.textContent, /已生成/);
});

test('HTTP 502 HTML and specific API errors are visible without reporting a URL', async () => {
    for (const [fetch, pattern] of [
        [async () => ({ok: false, status: 502, text: async () => '<html>Bad Gateway</html>'}), /非 JSON.*HTTP 502/],
        [async () => response(502, {error: '支付上游连接失败'}), /支付上游连接失败/],
        [async () => response(200, {success: true, url: 'https://evil.test/'}), /Stripe.*无效/],
        [async () => { throw Error('网络中断'); }, /网络中断/],
    ]) {
        const env = runtime();
        env.context.fetch = fetch;
        await env.context.submitAccountPoolTeamCheckout(env.options);
        assert.match(env.feedback.textContent, pattern);
        assert.equal(env.result.hidden, true);
        assert.equal(env.fieldset.disabled, false);
    }
});

test('copy writes the complete link and clipboard errors remain visible', async () => {
    const env = runtime();
    env.textarea.value = URL_VALUE;
    let copied;
    env.context.navigator.clipboard.writeText = async value => { copied = value; };
    await env.context.copyTeamCheckoutLink(env.options.dialog);
    assert.equal(copied, URL_VALUE);
    assert.match(env.feedback.textContent, /已复制/);
    env.context.navigator.clipboard.writeText = async () => { throw Error('权限不足'); };
    await env.context.copyTeamCheckoutLink(env.options.dialog);
    assert.match(env.feedback.textContent, /复制失败.*权限不足/);
});
