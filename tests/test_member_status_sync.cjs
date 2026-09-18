// Run with: node --test tests/test_member_status_sync.cjs
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const vm = require('node:vm');

const source = readFileSync(join(__dirname, '../app/static/js/main.js'), 'utf8');
function section(start, end) {
    return source.slice(source.indexOf(start), source.indexOf(end));
}

function harness() {
    const elements = new Map();
    const element = id => {
        if (!elements.has(id)) {
            const classes = new Set();
            elements.set(id, {
                value: '', textContent: '', innerHTML: '', disabled: false,
                classList: {contains: key => classes.has(key), toggle: (key, on) => on ? classes.add(key) : classes.delete(key)},
            });
        }
        return elements.get(id);
    };
    const timers = new Map(), calls = [], toasts = [];
    let timerId = 0;
    const context = vm.createContext({
        document: {getElementById: element, addEventListener() {}},
        window: {currentTeamId: 1},
        setTimeout: (fn, delay) => { const id = ++timerId; timers.set(id, {fn, delay}); return id; },
        clearTimeout: id => timers.delete(id),
        showToast: (...args) => toasts.push(args),
        showModal() {},
        hideModal() {},
        updateMemberInviteAvailability() {},
        renderMemberSeatSummary: () => '席位余额',
        renderMemberSeatControl: () => '',
        renderMemberSeatType: () => '',
        renderMemberAutoKickTime: () => '',
        renderMemberAuthorizationActions: () => '',
        escapeHtml: value => String(value),
        formatDateTime: () => '',
        getFriendlyAdminErrorMessage: value => value,
        apiCall: async url => { calls.push(url); return context.response; },
    });
    vm.runInContext(`
        let memberListRequestId = 0, memberSeatBalance = null, memberExistingEmails = new Set();
        ${section('let memberAuthorizationContext = null;', 'function generateMemberAuthorization()')}
        ${section('async function viewMembers(', 'async function revokeInvite(')}
        memberAuthorizationContext = {teamId: 1, email: 'member@example.com', busy: false, hasLink: true};
    `, context);
    return {context, element, calls, toasts, timers,
        run: expression => vm.runInContext(expression, context)};
}

function response(membership) {
    return {success: true, data: {success: true, data: {
        email: 'member@example.com', account_id: 'team-1', authorized: true,
        membership, can_export: membership === 'joined', message: '验证完成',
        members_snapshot: {
            success: true, joined_members: membership === 'joined' ? 2 : 1, total_seats: 2,
            members: [{email: 'member@example.com', status: membership, authorized: true}],
            seat_summary: {invites_complete: true}, seat_balance: {success: true},
        },
    }}};
}

test('callback immediately moves member and updates count using one snapshot', async () => {
    const h = harness();
    // The Team list reads data-id as a string; member icon buttons pass Number(data-team-id).
    h.context.response = {success: true, data: response('invited').data.data.members_snapshot};
    await h.run("viewMembers('1')");
    assert.equal(h.context.window.currentTeamId, 1);
    h.calls.length = 0;
    h.context.response = response('joined');
    h.element('memberAuthCallback').value = 'http://localhost:1455/auth/callback?code=test';
    await h.run("requestMemberAuthorization('callback')");
    assert.equal(h.calls.length, 1);
    assert.equal(h.element('team-member-count-1').textContent, '2/2');
    assert.match(h.element('modalJoinedMembersTableBody').innerHTML, /member@example.com/);
    assert.doesNotMatch(h.element('modalInvitedMembersTableBody').innerHTML, /member@example.com/);
    assert.equal(h.element('memberAuthStatus').classList.contains('is-success'), true);
    assert.equal(h.toasts[0][2].title, '邀请已完成');
    assert.equal(h.element('memberAuthExportBtn').disabled, false);
    assert.equal(h.timers.size, 0);
    await h.run("requestMemberAuthorization('check')");
    assert.equal(h.toasts.length, 1);
});

test('pending authorization polls until joined then stops and announces completion', async () => {
    const h = harness();
    h.context.response = response('invited');
    await h.run("requestMemberAuthorization('check', {notify:false})");
    assert.equal(h.element('team-member-count-1').textContent, '1/2');
    assert.match(h.element('modalInvitedMembersTableBody').innerHTML, /member@example.com/);
    assert.equal(h.toasts.length, 0);
    assert.equal(h.timers.size, 1);
    const timer = [...h.timers.values()][0];
    assert.equal(timer.delay, 5000);
    h.context.response = response('joined');
    timer.fn();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(h.element('team-member-count-1').textContent, '2/2');
    assert.equal(h.timers.size, 0);
    assert.equal(h.toasts.length, 1);
    assert.equal(h.calls.length, 2);
});

test('closing the dialog cancels polling but still applies an in-flight result', async () => {
    const h = harness();
    h.context.response = response('invited');
    await h.run("requestMemberAuthorization('check')");
    h.run('resetMemberAuthorization()');
    assert.equal(h.timers.size, 0);
    h.run("memberAuthorizationContext = {teamId:1, email:'member@example.com'}");
    let resolve;
    h.context.apiCall = () => new Promise(done => { resolve = done; });
    const pending = h.run("requestMemberAuthorization('check')");
    h.run('resetMemberAuthorization()');
    resolve(response('joined'));
    await pending;
    assert.equal(h.element('team-member-count-1').textContent, '2/2');
    assert.match(h.element('modalJoinedMembersTableBody').innerHTML, /member@example.com/);
    assert.equal(h.timers.size, 0);
});

test('late result updates its Team count without overwriting another Team dialog', async () => {
    const h = harness();
    let resolve;
    h.context.apiCall = () => new Promise(done => { resolve = done; });
    const pending = h.run("requestMemberAuthorization('check')");
    h.run('resetMemberAuthorization(); window.currentTeamId = 2');
    h.element('modalJoinedMembersTableBody').innerHTML = 'Team 2 members';
    resolve(response('joined'));
    await pending;
    assert.equal(h.element('team-member-count-1').textContent, '2/2');
    assert.equal(h.element('modalJoinedMembersTableBody').innerHTML, 'Team 2 members');
    assert.equal(h.toasts.length, 0);
});
