const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');

function harness(count = 4) {
    const updates = [], requests = [], handlers = {};
    const rows = Array.from({length: count}, (_, i) => ({
        dataset: {teamId: String(i + 1), seatBalanceState: 'pending'}, isConnected: true,
        querySelector() { return null; }, appendChild(node) { this.retry = node; },
    }));
    const context = vm.createContext({
        document: {
            querySelectorAll: () => rows.filter(row => row.dataset.seatBalanceState === 'pending'),
            addEventListener(name, fn) { handlers[name] = fn; },
            createElement: () => ({remove() {}}),
        }, window: {},
        updateTeamMemberCount: (...args) => updates.push(args),
        apiCall: url => new Promise(resolve => requests.push({url, resolve})),
    });
    vm.runInContext(readFileSync('app/static/js/team-seat-balances.js', 'utf8'), context);
    return {rows, updates, requests, handlers, refresh: context.window.teamSeatBalances.refresh};
}
const settle = () => new Promise(resolve => setImmediate(resolve));
const success = (remaining = 1, observed_at = '2026-10-24T14:00:00') => ({
    success: true, data: {success: true, observed_at, seat_balance: {success: true, balance: {remaining}}},
});

test('initial list load reads balances with at most two concurrent requests and does not enqueue twice', async () => {
    const env = harness();
    env.handlers.DOMContentLoaded();
    env.refresh();
    assert.equal(env.requests.length, 2);
    env.requests[0].resolve(success());
    await settle();
    assert.equal(env.requests.length, 3);
    assert.equal(env.updates.length, 1);
    assert.equal(env.rows[0].dataset.seatBalanceState, 'done');
    env.requests[1].resolve(success());
    await settle();
    assert.equal(env.requests.length, 4);
});

test('failed refresh keeps the balance and allows an explicit retry', async () => {
    const env = harness(1);
    env.refresh();
    env.requests[0].resolve({success: false});
    await settle();
    assert.equal(env.updates.length, 0);
    assert.equal(env.rows[0].dataset.seatBalanceState, 'failed');
    assert.match(env.rows[0].retry.textContent, /重试/);
    env.rows[0].retry.closest = () => env.rows[0];
    env.handlers.click({target: {closest: () => env.rows[0].retry}});
    assert.equal(env.requests.length, 2);
    env.requests[1].resolve(success());
    await settle();
    assert.equal(env.rows[0].dataset.seatBalanceState, 'done');
});

test('late responses cannot update a removed page or overwrite a newer member snapshot', async () => {
    const env = harness(3);
    env.refresh();
    env.rows[0].isConnected = false;
    env.rows[2].isConnected = false;
    env.rows[1].dataset.seatBalanceObservedAt = '2026-10-24T14:05:00';
    env.requests[0].resolve(success());
    env.requests[1].resolve(success());
    await settle();
    assert.equal(env.updates.length, 0);
    assert.equal(env.requests.length, 2);
});
