const {test} = require('node:test');
const assert = require('node:assert/strict');
const {harness} = require('./helpers/member-status-harness.cjs');

function setup({members = [], ...data} = {}) {
    const h = harness();
    const copied = [], reads = [];
    h.context.window.accountPoolCredentialStore = {
        load: async (email, options) => {
            reads.push({email, fresh: options.fresh});
            return {password: `password:${email}`, two_factor_secret: 'JBSWY3DPEHPK3PXP'};
        },
    };
    h.context.copyToClipboard = async text => { copied.push(text); return true; };
    h.context.snapshot = {success: true, members, ...data};
    h.run('applyMemberListSnapshot(1, snapshot)');
    return {...h, copied, reads, copy: () => h.run('window.invitedMemberCredentials.copy()')};
}

const invited = email => ({email, status: 'invited'});

test('copies all invited accounts in order with fresh passwords and 2FA secrets, excluding joined members', async () => {
    const h = setup({members: [
        invited('one@example.com'), {email: 'owner@example.com', status: 'joined'},
        invited('two@example.com'),
    ]});
    assert.equal(h.element('copyInvitedMemberCredentialsBtn').disabled, false);
    await h.copy();
    assert.deepEqual(h.reads, [
        {email: 'one@example.com', fresh: true}, {email: 'two@example.com', fresh: true},
    ]);
    assert.deepEqual(h.copied, [
        'one@example.com----password:one@example.com----JBSWY3DPEHPK3PXP\n'
        + 'two@example.com----password:two@example.com----JBSWY3DPEHPK3PXP',
    ]);
});

test('empty, partial, or failed invitation lists cannot be copied', async () => {
    for (const data of [
        {},
        {members: [invited('partial@example.com')], pagination: {has_more: true}},
        {members: [invited('failed@example.com')], seat_summary: {invites_complete: false}},
    ]) {
        const h = setup(data);
        assert.equal(h.element('copyInvitedMemberCredentialsBtn').disabled, true);
        await h.copy();
        assert.deepEqual(h.copied, []);
        assert.deepEqual(h.reads, []);
    }
});

test('missing credentials preserve every account and explicitly report empty fields', async () => {
    const h = setup({members: [invited('empty@example.com'), invited('password-only@example.com')]});
    h.context.window.accountPoolCredentialStore.load = async email => ({
        password: email.startsWith('password-only') ? 'saved-password' : '', two_factor_secret: '',
    });
    await h.copy();
    assert.deepEqual(h.copied, ['empty@example.com--------\npassword-only@example.com----saved-password----']);
    assert.match(h.toasts[0][0], /已复制 2 个账号；2 个账号未保存密码或 2FA/);
    assert.equal(h.toasts[0][1], 'warning');
});

test('credential read failure reports the account without writing a partial clipboard', async () => {
    const h = setup({members: [invited('ok@example.com'), invited('missing@example.com')]});
    h.context.window.accountPoolCredentialStore.load = async email => {
        if (email === 'missing@example.com') throw new Error('该邮箱不在账号号池中');
        return {password: 'saved', two_factor_secret: 'JBSWY3DPEHPK3PXP'};
    };
    await h.copy();
    assert.deepEqual(h.copied, []);
    assert.match(h.toasts[0][0], /missing@example.com：该邮箱不在账号号池中/);
    assert.equal(h.toasts[0][1], 'error');
    assert.equal(h.element('copyInvitedMemberCredentialsBtn').disabled, false);
});

test('copying prevents duplicate clicks and restores the button after completion', async () => {
    const h = setup({members: [invited('one@example.com')]});
    let resolve;
    h.context.window.accountPoolCredentialStore.load = () => new Promise(done => { resolve = done; });
    const pending = h.copy();
    assert.equal(h.element('copyInvitedMemberCredentialsBtn').disabled, true);
    assert.match(h.element('copyInvitedMemberCredentialsLabel').textContent, /正在读取/);
    await h.copy();
    resolve({password: 'saved', two_factor_secret: 'JBSWY3DPEHPK3PXP'});
    await pending;
    assert.equal(h.copied.length, 1);
    assert.equal(h.element('copyInvitedMemberCredentialsBtn').disabled, false);
    assert.equal(h.element('copyInvitedMemberCredentialsLabel').textContent, '批量复制账号');
});

test('switching Teams, refreshing, or closing during reads prevents copying outdated accounts', async () => {
    for (const change of [
        'window.currentTeamId = 2',
        'prepareMemberList(1)',
        '++memberListRequestId',
        'applyMemberListSnapshot(1, {members: []})',
    ]) {
        const h = setup({members: [invited('old@example.com')]});
        let resolve;
        h.context.window.accountPoolCredentialStore.load = () => new Promise(done => { resolve = done; });
        const pending = h.copy();
        h.run(change);
        resolve({password: 'saved', two_factor_secret: 'JBSWY3DPEHPK3PXP'});
        await pending;
        assert.deepEqual(h.copied, []);
        assert.match(h.toasts[0][0], /成员列表已变化/);
    }
});

test('clipboard write errors are exposed without reporting success', async () => {
    const h = setup({members: [invited('one@example.com')]});
    h.context.copyToClipboard = async () => { throw new Error('clipboard denied'); };
    await h.copy();
    assert.match(h.toasts[0][0], /批量复制失败：clipboard denied/);
    assert.equal(h.toasts[0][1], 'error');
    assert.equal(h.element('copyInvitedMemberCredentialsBtn').disabled, false);
});
