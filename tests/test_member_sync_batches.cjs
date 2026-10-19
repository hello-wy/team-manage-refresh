const {test} = require('node:test');
const assert = require('node:assert/strict');
const {harness} = require('./helpers/member-status-harness.cjs');

function batch({email, cursor = null, loaded = 1, total = 2}) {
    return {success: true, data: {
        success: true, members: [{email, status: 'joined'}], joined_members: total,
        pagination: {has_more: cursor !== null, next_cursor: cursor, loaded, total},
    }};
}

test('opening always starts a new sync and renders batches before completion', async () => {
    const h = harness();
    const requests = [];
    let resolveFinal;
    h.context.apiCall = async (url, options) => {
        requests.push({url, ...JSON.parse(options.body), cache: options.cache});
        if (requests.length === 1) return batch({email: 'first@example.com', cursor: 'next'});
        return new Promise(resolve => { resolveFinal = resolve; });
    };
    const pending = h.run('viewMembers(1)');
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(requests.map(request => request.cursor), [null, 'next']);
    assert.equal(requests[0].url, '/admin/teams/1/members/sync');
    assert.equal(requests[0].cache, 'no-store');
    assert.match(h.element('modalJoinedMembersTableBody').innerHTML, /first@example.com/);
    assert.match(h.element('memberSyncStatus').textContent, /1\/2/);
    resolveFinal(batch({email: 'latest@example.com', loaded: 2}));
    await pending;
    assert.match(h.element('modalJoinedMembersTableBody').innerHTML, /latest@example.com/);
    assert.match(h.element('memberSyncStatus').textContent, /已同步/);
    h.context.apiCall = async (url, options) => {
        assert.equal(JSON.parse(options.body).cursor, null);
        return batch({email: 'reopened@example.com'});
    };
    await h.run('viewMembers(1)');
    assert.match(h.element('modalJoinedMembersTableBody').innerHTML, /reopened@example.com/);
});

test('a failed continuation exposes the error and preserves the completed batch', async () => {
    const h = harness();
    let count = 0;
    h.context.apiCall = async () => ++count === 1
        ? batch({email: 'first@example.com', cursor: 'next'})
        : {success: false, error: 'upstream timeout'};
    await h.run('viewMembers(1)');
    assert.equal(count, 2);
    assert.match(h.element('memberSyncStatus').textContent, /同步失败.*upstream timeout/);
    assert.match(h.element('modalJoinedMembersTableBody').innerHTML, /first@example.com/);
    assert.equal(h.toasts[0][1], 'error');
});

test('switching Teams ignores the old batch and does not fetch another page', async () => {
    const h = harness();
    let resolve;
    let count = 0;
    h.context.apiCall = () => {
        count += 1;
        return new Promise(done => { resolve = done; });
    };
    const pending = h.run('viewMembers(1)');
    h.run('window.currentTeamId = 2');
    h.element('modalJoinedMembersTableBody').innerHTML = 'Team 2';
    resolve(batch({email: 'wrong@example.com', cursor: 'next'}));
    await pending;
    assert.equal(count, 1);
    assert.equal(h.element('modalJoinedMembersTableBody').innerHTML, 'Team 2');
});

test('closing and reopening invalidates the old request even for the same Team', async () => {
    const h = harness();
    let resolveOld;
    h.context.apiCall = () => new Promise(resolve => { resolveOld = resolve; });
    const old = h.run('viewMembers(1)');
    h.run('++memberListRequestId');
    h.context.apiCall = async () => batch({email: 'new@example.com'});
    await h.run('viewMembers(1)');
    resolveOld(batch({email: 'old@example.com', cursor: 'old-next'}));
    await old;
    assert.match(h.element('modalJoinedMembersTableBody').innerHTML, /new@example.com/);
    assert.doesNotMatch(h.element('modalJoinedMembersTableBody').innerHTML, /old@example.com/);
});

test('unchanged continuation cursor fails explicitly instead of looping', async () => {
    const h = harness();
    h.context.response = batch({email: 'member@example.com', cursor: 'same'});
    await h.run('viewMembers(1)');
    assert.equal(h.calls.length, 2);
    assert.match(h.element('memberSyncStatus').textContent, /同步失败.*下一批位置/);
});

test('first batch failure replaces the loading placeholder with the error', async () => {
    const h = harness();
    h.context.response = {success: false, error: '成员列表分页不完整'};
    await h.run('viewMembers(1)');
    for (const id of ['modalJoinedMembersTableBody', 'modalInvitedMembersTableBody']) {
        assert.match(h.element(id).innerHTML, /成员列表分页不完整/);
        assert.doesNotMatch(h.element(id).innerHTML, /加载中/);
    }
});
