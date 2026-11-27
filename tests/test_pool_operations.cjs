const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const poolSource = readFileSync('app/static/js/account-pool.js', 'utf8');
const pickerSource = poolSource.slice(poolSource.indexOf('function accountPoolTeamLabel'), poolSource.indexOf('async function inviteAccountPoolEntry'));
const team = (id, standard, premium) => ({id, team_name: 'Team ' + id, email: 'owner@example.com', status: 'full',
    seat_balance_needs_refresh: false, seat_snapshot: {standard_paid: 10, standard_remaining: standard, premium_paid: 3, premium_remaining: premium}});
function pickerRuntime(fetch) {
    const elements = Object.fromEntries(['accountPoolTeamPickerOptions', 'accountPoolTeamPickerEmail', 'accountPoolTeamPickerTitle', 'accountPoolInviteSeatPicker', 'accountPoolInviteSeatType']
        .map(id => [id, {innerHTML: '', textContent: '', querySelectorAll: () => [], appendChild() {}}]));
    const context = vm.createContext({window: {}, document: {getElementById: id => elements[id], createElement: () => ({addEventListener() {}})},
        escapeHtml: value => String(value).replaceAll('<', '&lt;'), showModal() {}, hideModal() {}, fetch});
    vm.runInContext(pickerSource, context);
    return {context, elements};
}
test('Team invitations use requested seat balance instead of legacy total capacity', () => {
    const {context: c} = pickerRuntime();
    const standard = c.accountPoolPickerCard(team(1, 2, 0), 'invite', 'default');
    const premium = c.accountPoolPickerCard(team(1, 2, 0), 'invite', 'premium');
    assert.doesNotMatch(standard, / disabled/);
    assert.match(standard, /标准 剩余 2 \/ 10/);
    assert.match(premium, / disabled/);
    assert.match(premium, /高级 剩余 0 \/ 3/);
    assert.match(premium, /高级席位不足/);
    const unknown = c.accountPoolPickerCard({id: 2, current_members: 1, max_members: 50}, 'invite', 'default');
    assert.match(unknown, / disabled/);
    assert.match(unknown, /待同步/);
    assert.doesNotMatch(unknown, /1\/50/);
});
test('late seat requests cannot replace a newly opened Team picker', async () => {
    let resolve;
    const env = pickerRuntime(() => new Promise(done => { resolve = done; }));
    env.context.openAccountPoolTeamPicker(1, 'old@example.com', [{id: 1}], 'invite', 'default');
    env.context.openAccountPoolTeamPicker(2, 'new@example.com', [team(2, 4, 1)], 'invite', 'premium');
    resolve({ok: true, json: async () => ({success: true, seat_balance: {balance: {
        standard: {known: true, paid: 10, remaining: 9}, premium: {known: true, paid: 3, remaining: 2}}}})});
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(env.elements.accountPoolTeamPickerEmail.textContent, 'new@example.com');
    assert.match(env.elements.accountPoolTeamPickerOptions.innerHTML, /data-team-id="2"/);
    assert.doesNotMatch(env.elements.accountPoolTeamPickerOptions.innerHTML, /data-team-id="1"/);
});
test('invitation requires an explicit seat and updates Team availability when switched', () => {
    const {context:c, elements} = pickerRuntime();
    c.openAccountPoolTeamPicker(1, 'member@example.com', [team(1, 2, 0), team(2, 0, 2)], 'invite');
    const select = elements.accountPoolInviteSeatType;
    assert.equal(select.value, '');
    assert.equal(elements.accountPoolInviteSeatPicker.hidden, false);
    assert.equal((elements.accountPoolTeamPickerOptions.innerHTML.match(/ disabled/g) || []).length, 2);
    assert.doesNotMatch(elements.accountPoolTeamPickerOptions.innerHTML, /is-requested/);
    select.value = 'premium'; select.onchange();
    assert.match(elements.accountPoolTeamPickerOptions.innerHTML, /data-team-id="1" disabled/);
    assert.doesNotMatch(elements.accountPoolTeamPickerOptions.innerHTML, /data-team-id="2" disabled/);
    select.value = 'default'; select.onchange();
    assert.doesNotMatch(elements.accountPoolTeamPickerOptions.innerHTML, /data-team-id="1" disabled/);
    assert.match(elements.accountPoolTeamPickerOptions.innerHTML, /data-team-id="2" disabled/);
    c.openAccountPoolTeamPicker(2, 'other@example.com', [team(2, 2, 2)], 'invite');
    assert.equal(select.value, '');
    c.openAccountPoolTeamPicker(2, 'other@example.com', [team(2, 2, 2)], 'automatic_login');
    assert.equal(elements.accountPoolInviteSeatPicker.hidden, true);
});
test('invite request preserves the chosen premium seat and blocks an unspecified seat', async () => {
    const requests = [];
    const context = vm.createContext({window:{}, showToast(){}, refreshAccountPoolTable:async()=>{},
        confirmAccountPoolAction:async()=>true,
        fetch:async(url, options)=>{requests.push(JSON.parse(options.body)); return {ok:true,json:async()=>({success:true})};}});
    vm.runInContext(poolSource.slice(poolSource.indexOf('async function inviteAccountPoolEntry'), poolSource.indexOf('async function openAccountPoolInvitePicker')), context);
    await context.inviteAccountPoolEntry(1, 'member@example.com', 2, '', {});
    assert.equal(requests.length, 0);
    await context.inviteAccountPoolEntry(1, 'member@example.com', 2, 'premium', {});
    assert.deepEqual(requests, [{team_id:2, seat_type:'premium'}]);
});
const main = readFileSync('app/static/js/main.js', 'utf8');
test('billing rejection is explained without treating its HTTP 401 as a login failure', () => {
    const context = vm.createContext({});
    vm.runInContext(main.slice(main.indexOf('function extractErrorText'), main.indexOf('function cleanupLegacyThemeSettingsSection')), context);
    for (const error of ['workspace_subscription_delinquent', {detail: {code: 'workspace_subscription_delinquent', message: 'Team has a delinquent subscription'}}]) {
        const message = context.getFriendlyAdminErrorMessage(error, 401, 'member');
        assert.match(message, /订阅账单异常/);
        assert.match(message, /检查并处理账单/);
        assert.doesNotMatch(message, /登录.*失效|重新登录/);
    }
});
const selectionSource = main.slice(main.indexOf('function getMemberInviteEmails'), main.indexOf('function appendMemberPoolOptions'));
function selectionRuntime(inputs) {
    let validations = 0;
    const label = {};
    const context = vm.createContext({
        parseMemberEmails: text => text.split(/[\s,]+/).filter(Boolean),
        updateMemberInviteAvailability: () => validations++,
        document: {querySelectorAll: query => query.endsWith('input:checked') ? inputs.filter(x => x.checked) : inputs,
                   getElementById: () => label},
    });
    vm.runInContext(selectionSource, context);
    return {context, label, count: () => validations};
}
test('batch invitation combines checked accounts and pasted emails without duplicates', () => {
    const {context} = selectionRuntime([]);
    const result = context.getMemberInviteEmails({memberEmails: {value: 'a@example.com\nb@example.com'},
        querySelectorAll: () => [{value: 'A@example.com'}, {value: 'c@example.com'}]});
    assert.deepEqual(Array.from(result), ['A@example.com', 'b@example.com', 'c@example.com']);
});
test('select-all affects visible accounts and clear removes hidden selections too', () => {
    const visible = {value: 'a@example.com', checked: false, closest: () => ({hidden: false})};
    const hidden = {value: 'b@example.com', checked: false, closest: () => ({hidden: true})};
    const env = selectionRuntime([visible, hidden]);
    env.context.selectMemberPoolEmails(true);
    assert.equal(visible.checked, true); assert.equal(hidden.checked, false);
    assert.equal(env.label.textContent, '已选 1 个');
    hidden.checked = true;
    env.context.selectMemberPoolEmails(false);
    assert.equal(visible.checked, false); assert.equal(hidden.checked, false);
    assert.equal(env.label.textContent, '已选 0 个');
});
