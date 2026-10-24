// Run with: node --test tests/test_member_status_sync.cjs
const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');

const {harness, section} = require('./helpers/member-status-harness.cjs');

function response(membership) {
    return {success: true, data: {success: true, data: {
        email: 'member@example.com', account_id: 'team-1', authorized: true,
        membership, can_export: membership === 'joined', message: '验证完成',
        members_snapshot: {
            success: true, joined_members: membership === 'joined' ? 2 : 1, total_seats: 2,
            team_status: membership === 'joined' ? 'full' : 'active',
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
    assert.equal(h.element('team-status-1').textContent, '已满');
    assert.equal(h.element('team-status-1').className, 'status-badge status-full');
    assert.match(h.element('modalJoinedMembersTableBody').innerHTML, /member@example.com/);
    assert.doesNotMatch(h.element('modalInvitedMembersTableBody').innerHTML, /member@example.com/);
    assert.equal(h.element('memberAuthStatus').classList.contains('is-success'), true);
    assert.equal(h.toasts[0][2].title, '邀请已完成');
    assert.equal(h.element('memberAuthExportBtn').disabled, false);
    assert.equal(h.element('memberAuthImportBtn').disabled, false);
    assert.equal(h.timers.size, 0);
    await h.run("requestMemberAuthorization('check')");
    assert.equal(h.toasts.length, 1);
});

test('pending authorization polls until joined then stops and announces completion', async () => {
    const h = harness();
    h.context.response = response('invited');
    await h.run("requestMemberAuthorization('check', {notify:false})");
    assert.equal(h.element('team-member-count-1').textContent, '1/2');
    assert.equal(h.element('team-status-1').textContent, '可用');
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

test('sub2api import starts immediately without confirmation', async () => {
    const requests = [];
    const context = vm.createContext({
        memberAuthorizationContext: null,
        confirm: () => { throw new Error('confirmation should not be requested'); },
        fetch: async (...args) => {
            requests.push(args);
            return {
                ok: true,
                json: async () => ({success: true, data: {account_id: 42, group_count: 2}}),
            };
        },
        showToast() {},
        markMemberSub2apiExported() {},
    });
    vm.runInContext(
        section('async function importMemberSub2api(', 'async function exportMemberSub2api('),
        context,
    );

    await vm.runInContext("importMemberSub2api(7, 'member@example.com')", context);

    assert.equal(requests.length, 1);
    assert.equal(requests[0][0], '/admin/teams/7/members/authorization/import-sub2api');
});

test('member list renders name and profile sync errors as escaped text', async () => {
    const h = harness();
    h.context.escapeHtml = value => String(value).replaceAll('<', '&lt;').replaceAll('>', '&gt;');
    h.context.response = {success: true, data: {
        success: true, members: [{status: 'joined', email: 'member@example.com',
            name: '<成员甲>', profile_sync_error: '<缺少加入时间>'}],
    }};
    await h.run('viewMembers(1)');
    const html = h.element('modalJoinedMembersTableBody').innerHTML;
    assert.match(html, /member-name.*&lt;成员甲&gt;/);
    assert.match(html, /&lt;缺少加入时间&gt;/);
    assert.doesNotMatch(html, /<成员甲>|<缺少加入时间>/);
});

test('exit schedule cell distinguishes owner, non-participating, pending and scheduled members', () => {
    const context = vm.createContext({escapeHtml: value => value, formatDateTime: value => value, Date});
    vm.runInContext(section('function renderMemberAutoKickTime(', 'function renderMemberAutoKickControl('), context);
    const render = member => context.renderMemberAutoKickTime(member);
    assert.match(render({role: 'account-owner'}), /不会自动下线/);
    assert.match(render({auto_kick_enabled: false, auto_kick_at: '2026-10-15T15:13:00'}), /不参与/);
    assert.match(render({auto_kick_enabled: true}), /等待同步加入时间/);
    assert.match(render({auto_kick_enabled: true, auto_kick_at: '2026-10-15T15:13:00'}), /2026-10-15T15:13:00/);
});
