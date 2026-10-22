const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const {section} = require('./helpers/member-status-harness.cjs');

const JOINED_AT = '2026-10-15T13:13:00';
const DEADLINE = '2026-10-15T15:13:00';

function controls() {
    const classes = new Set(['text-muted']);
    const time = {textContent: '不会自动下线', classList: {
        toggle: (key, on) => on ? classes.add(key) : classes.delete(key),
    }};
    const icon = {setAttribute: (key, value) => { icon[key] = value; }};
    const row = {dataset: {joinedAt: JOINED_AT}, querySelector: () => time};
    const button = {dataset: {exempt: 'false'}, closest: () => row,
        querySelector: () => icon, setAttribute: (key, value) => { button[key] = value; }};
    const context = vm.createContext({window: {}, Date,
        escapeHtml: String, formatDateTime: String,
        document: {querySelectorAll: () => [row]},
    });
    vm.runInContext(section('function renderMemberAutoKickTime(', 'async function toggleMemberAutoKickExemption('), context);
    vm.runInContext(section('function applyMemberAutoKickRow(', 'function memberSeatQuota('), context);
    return {context, button, time, classes};
}

test('exempt standard seat keeps a time element and manual-enable control', () => {
    const {context} = controls();
    const member = {user_id: 'member', seat_type: 'default', auto_kick_exempt: true};
    assert.match(context.renderMemberAutoKickTime(member), /member-auto-kick-time text-muted/);
    assert.match(context.renderMemberAutoKickTime(member), /不会自动下线/);
    assert.match(context.renderMemberAutoKickControl(1, member), /data-exempt="false"/);
    assert.equal(context.renderMemberAutoKickControl(1, {...member, role: 'account-owner'}), '');
});

test('manual enable and disable update the displayed time and control together', () => {
    const {context, button, time, classes} = controls();
    context.applyMemberAutoKickRow(button, {exempt: false, auto_kick_at: DEADLINE});
    assert.equal(time.textContent, DEADLINE);
    assert.equal(button.dataset.exempt, 'true');
    assert.equal(classes.has('text-muted'), false);
    context.applyMemberAutoKickRow(button, {exempt: true, auto_kick_at: null});
    assert.equal(time.textContent, '不会自动下线');
    assert.equal(button.dataset.exempt, 'false');
    assert.equal(classes.has('text-muted'), true);
    assert.equal(classes.has('is-overdue'), false);
});

test('changing Team hours leaves exempt standard seats exempt', () => {
    const {context, time} = controls();
    context.updateMemberAutoKickTimes(6);
    assert.equal(time.textContent, '不会自动下线');
});

test('enabling without a deadline exposes missing schedule and can be repaired in place', () => {
    const {context, button, time, classes} = controls();
    context.applyMemberAutoKickRow(button, {exempt: false, auto_kick_at: null});
    assert.match(time.textContent, /下线计划未生成/);
    assert.equal(classes.has('text-danger'), true);
    context.applyMemberAutoKickRow(button, {exempt: false, auto_kick_at: DEADLINE});
    assert.equal(time.textContent, DEADLINE);
    assert.equal(classes.has('text-danger'), false);
});
