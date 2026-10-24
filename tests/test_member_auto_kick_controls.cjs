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
    const member = {user_id: 'member', email: 'member@example.com', auto_kick_enabled: true};
    assert.match(ctx.renderMemberAutoKickControl(1, member), /data-enabled="true"/);
    assert.match(ctx.renderMemberAutoKickControl(1, member), /不参与自动下线/);
    assert.match(ctx.renderMemberAutoKickControl(1, {...member, auto_kick_enabled: false}), /data-enabled="false"/);
    assert.match(ctx.renderMemberAutoKickControl(1, {...member, auto_kick_enabled: false}), /title="参与自动下线"/);
    assert.equal(ctx.renderMemberAutoKickControl(1, {...member, role: 'account-owner'}), '');
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
