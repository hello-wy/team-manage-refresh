const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const {section} = require('./helpers/member-status-harness.cjs');

function context(extra = {}) {
    const ctx = vm.createContext({
        Date,
        escapeHtml: value => String(value).replace(/</g, '&lt;').replace(/>/g, '&gt;'),
        formatDateTime: value => String(value),
        ...extra,
    });
    vm.runInContext(section('function renderMemberAutoKickTime(', 'async function toggleMemberAutoKickParticipation('), ctx);
    return ctx;
}

test('participation toggle reflects current state and is hidden for owners', () => {
    const ctx = context();
    const member = {user_id: 'member', email: 'member@example.com', seat_type: 'premium', auto_kick_enabled: true};
    assert.match(ctx.renderMemberAutoKickControl(1, member), /data-enabled="true"/);
    assert.match(ctx.renderMemberAutoKickControl(1, member), /不参与自动下线/);
    assert.match(ctx.renderMemberAutoKickControl(1, {...member, auto_kick_enabled: false}), /data-enabled="false"/);
    assert.match(ctx.renderMemberAutoKickControl(1, {...member, auto_kick_enabled: false}), /title="参与自动下线"/);
    assert.equal(ctx.renderMemberAutoKickControl(1, {...member, role: 'account-owner'}), '');
    assert.equal(ctx.renderMemberAutoKickControl(1, {...member, status: 'invited'}), '');
    assert.match(ctx.renderMemberAutoKickControl(1, {...member, seat_type: 'prolite'}), /不参与自动下线/);
});

test('failed kick shows escaped reason, attempt count and retry time', () => {
    const ctx = context();
    const html = ctx.renderMemberAutoKickTime({
        user_id: 'member', email: 'member@example.com', auto_kick_enabled: true, auto_kick_at: '2026-10-24T10:00:00',
        auto_kick_failures: 2, auto_kick_last_error: '<b>凭据失效</b>', auto_kick_retry_at: '2026-10-24T10:35:00',
    });
    assert.match(html, /member-auto-kick-error/);
    assert.match(html, /第 2 次踢出失败：&lt;b&gt;凭据失效&lt;\/b&gt;，2026-10-24T10:35:00 重试/);
    assert.doesNotMatch(html, /<b>/);
    assert.doesNotMatch(ctx.renderMemberAutoKickTime({auto_kick_enabled: true, auto_kick_at: '2026-10-24T10:00:00'}), /踢出失败/);
});

test('standard departure is fixed to Team expiry and offers no hourly toggle', () => {
    const ctx = context();
    const member = {user_id: 'member', email: 'standard@example.com', departure_policy: 'standard_expiry',
        auto_kick_at: '2026-11-20T12:00:00', auto_kick_enabled: false};
    assert.match(ctx.renderMemberAutoKickTime(member), />2026-11-20T12:00:00<\/span>/);
    assert.doesNotMatch(ctx.renderMemberAutoKickTime(member), /Team 到期下线|不参与/);
    assert.equal(ctx.renderMemberAutoKickControl(1, member), '');
    assert.match(ctx.renderMemberAutoKickTime({...member, auto_kick_at: null}), /Team 到期时间待同步/);
    assert.match(ctx.renderMemberAutoKickTime({...member, status: 'invited', seat_type: 'default'}), /接受邀请后保留至 Team 到期/);
});

test('all occupied standard seats are locked while premium seats can change', () => {
    const ctx = context({canAllocateMemberSeat: () => true});
    vm.runInContext(`let memberSeatUpdateInProgress = false;
${section('function renderMemberSeatType(', 'async function saveMemberSeatType(')}`, ctx);
    const member = {user_id: 'member', email: 'warranty@example.com', seat_type: 'standard', warranty_protected: true};
    const html = ctx.renderMemberSeatControl(member);
    assert.match(html, /标准席位/);
    assert.doesNotMatch(html, /<select/);
    for (const seat_type of ['default', 'standard', ' STANDARD ']) {
        assert.doesNotMatch(ctx.renderMemberSeatControl({...member, seat_type, warranty_protected: false}), /<select|保存|不参与/);
    }
    assert.match(ctx.renderMemberSeatControl({...member, seat_type: 'prolite', warranty_protected: false}), /<select/);
    const pending = ctx.renderMemberSeatControl({...member, pending_seat_type: 'premium'});
    assert.match(pending, /待生效/);
    assert.doesNotMatch(pending, /<select/);
});

test('standard type overrides legacy hourly policy and participation flags', () => {
    const ctx = context();
    for (const seat_type of ['default', 'standard', ' STANDARD ']) {
        const member = {seat_type, user_id: 'member', email: 'standard@example.com', departure_policy: 'timed',
            auto_kick_enabled: false, auto_kick_at: '2026-10-20T12:00:00', team_expires_at: '2026-11-20T12:00:00'};
        assert.match(ctx.renderMemberAutoKickTime(member), />2026-11-20T12:00:00<\/span>/);
        assert.equal(ctx.renderMemberAutoKickControl(1, member), '');
        assert.match(ctx.renderMemberAutoKickTime({...member, team_expires_at: null}), /到期时间待同步/);
    }
});
