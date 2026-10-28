const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');

const NOW = Date.parse('2026-10-28T04:00:00Z');
const MINUTE_MS = 60_000;

function dateButton(renewalAt = '2026-10-30T15:04:00+08:00') {
    const attributes = {'aria-pressed': 'false'};
    return {
        dataset: {renewalAt, renewalDate: '2026-10-30 15:04'},
        textContent: '2026-10-30 15:04', title: '',
        getAttribute: name => attributes[name],
        setAttribute(name, value) { attributes[name] = value; },
    };
}

function harness() {
    let now = NOW;
    let buttons = [dateButton()];
    const handlers = {}, timers = new Map();
    let nextTimer = 0;
    const context = vm.createContext({
        document: {
            addEventListener(name, fn) { handlers[name] = fn; },
            querySelectorAll: () => buttons.filter(button => button.getAttribute('aria-pressed') === 'true'),
        },
        Date: {parse: Date.parse, now: () => now},
        setInterval(fn, delay) { timers.set(++nextTimer, {fn, delay}); return nextTimer; },
        clearInterval(id) { timers.delete(id); },
    });
    vm.runInContext(readFileSync('app/static/js/team-renewal-time.js', 'utf8'), context);
    return {
        first: buttons[0], timers,
        click: button => handlers.click({target: {closest: () => button}}),
        advance(milliseconds) { now += milliseconds; timers.forEach(timer => timer.fn()); },
        replaceRows(next) { buttons = next; },
    };
}

test('dates stay visible until clicked, then toggle to a live countdown and back', () => {
    const env = harness();
    assert.equal(env.first.textContent, '2026-10-30 15:04');
    assert.equal(env.timers.size, 0);
    env.click(env.first);
    assert.equal(env.first.textContent, '剩余 2 天 3 小时 4 分钟');
    assert.equal(env.first.getAttribute('aria-pressed'), 'true');
    assert.match(env.first.title, /2026-10-30 15:04（北京时间）/);
    env.advance(MINUTE_MS);
    assert.equal(env.first.textContent, '剩余 2 天 3 小时 3 分钟');
    env.click(env.first);
    assert.equal(env.first.textContent, '2026-10-30 15:04');
    assert.equal(env.first.getAttribute('aria-pressed'), 'false');
    assert.equal(env.timers.size, 0);
});

test('explicit timezone offsets identify the same instant and rollover stays accurate', () => {
    const env = harness();
    const beijing = dateButton('2026-10-29T12:00:00+08:00');
    const pacific = dateButton('2026-10-28T21:00:00-07:00');
    env.replaceRows([beijing, pacific]);
    env.click(beijing);
    env.click(pacific);
    assert.equal(beijing.textContent, '剩余 1 天 0 小时 0 分钟');
    assert.equal(pacific.textContent, beijing.textContent);
    assert.equal(env.timers.size, 1);
    env.advance(MINUTE_MS);
    assert.equal(beijing.textContent, '剩余 0 天 23 小时 59 分钟');
});

test('the last partial minute remains visible and the deadline does not claim a payment', () => {
    const env = harness();
    const button = dateButton('2026-10-28T12:00:20+08:00');
    env.replaceRows([button]);
    env.click(button);
    assert.equal(button.textContent, '剩余 0 天 0 小时 1 分钟');
    env.advance(MINUTE_MS);
    assert.equal(button.textContent, '已到续费时间');
    env.advance(MINUTE_MS);
    assert.equal(button.textContent, '已到续费时间');
    env.click(button);
    assert.equal(button.textContent, button.dataset.renewalDate);
});

test('replaced table rows start with dates and remain clickable without duplicate timers', () => {
    const env = harness();
    env.click(env.first);
    const replacement = dateButton();
    env.replaceRows([replacement]);
    env.advance(MINUTE_MS);
    assert.equal(env.timers.size, 0);
    assert.equal(replacement.textContent, replacement.dataset.renewalDate);
    env.click(replacement);
    assert.equal(replacement.textContent, '剩余 2 天 3 小时 3 分钟');
    assert.equal(env.timers.size, 1);
    env.click(null);
    assert.equal(env.timers.size, 1);
});

test('invalid timestamps expose an explicit error instead of a misleading countdown', () => {
    const env = harness();
    const button = dateButton('invalid-time');
    env.replaceRows([button]);
    assert.throws(() => env.click(button), /续费时间无效/);
});
