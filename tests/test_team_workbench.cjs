const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const {harness: memberHarness} = require('./helpers/member-status-harness.cjs');

function menuHarness() {
    const documentListeners = {}, windowListeners = {}, calls = [];
    let activeElement;
    const items = Array.from({length: 3}, () => ({focus(options) { activeElement = this; this.options = options; }}));
    const body = {appendChild(node) { node.parentElement = body; }};
    const menu = {
        hidden: true, parentElement: {}, innerHTML: '', style: {}, offsetWidth: 244, offsetHeight: 300,
        setAttribute() {}, showPopover() { this.popoverOpen = true; },
        hidePopover() { this.popoverOpen = false; }, matches() { return this.popoverOpen; },
        querySelector: () => items[0], querySelectorAll: () => items,
        contains: target => target === menu || items.includes(target),
    };
    const document = {body, getElementById: () => menu, get activeElement() { return activeElement; },
        addEventListener: (name, fn) => { documentListeners[name] = fn; }};
    const window = {innerWidth: 800, innerHeight: 600, addEventListener: (name, fn) => { windowListeners[name] = fn; }};
    const context = vm.createContext({document, window, closeFloatingDropdowns() {},
        escapeHtml: value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;'),
        exportTeamJson: (...args) => calls.push(args),
        openTeamSeatInvite: (...args) => calls.push(args),
    });
    vm.runInContext(readFileSync('app/static/js/team-workbench.js', 'utf8'), context);
    const trigger = (id, top = 450) => ({
        dataset: {id, email: `owner${id}@example.com`},
        attributes: {}, isConnected: true,
        setAttribute(key, value) { this.attributes[key] = value; }, focus(options) { this.focused = options; },
        getBoundingClientRect: () => ({right: 790, top, bottom: top + 36}),
        closest(selector) { return selector === '#teamTableRegion .btn-team-more' ? this : null; },
    });
    const click = target => documentListeners.click({target});
    return {menu, trigger, click, items, calls, context, body, documentListeners, windowListeners,
        key(key) { documentListeners.keydown({key, target: activeElement, preventDefault() {}}); }};
}

test('more menu is mounted above the table and stays inside the viewport without scrolling on focus', () => {
    const env = menuHarness(), button = env.trigger('1');
    env.click(button);
    assert.equal(env.menu.parentElement, env.body);
    assert.equal(env.menu.hidden, false);
    assert.equal(env.menu.popoverOpen, true);
    assert.doesNotMatch(env.menu.innerHTML, /device-auth|设备验证/);
    assert.equal(button.attributes['aria-expanded'], 'true');
    assert.equal(env.items[0].options.preventScroll, true);
    assert.ok(parseFloat(env.menu.style.top) + env.menu.offsetHeight <= 588);
    assert.ok(parseFloat(env.menu.style.left) + env.menu.offsetWidth <= 788);
    env.windowListeners.scroll({target: env.menu});
    assert.equal(env.menu.hidden, false);
    env.windowListeners.scroll({target: {}});
    assert.equal(env.menu.hidden, true);
    assert.equal(button.attributes['aria-expanded'], 'false');
});

test('switching rows, keyboard navigation, escape and reopening preserve menu state', () => {
    const env = menuHarness(), first = env.trigger('1'), second = env.trigger('2', 20);
    env.click(first);
    env.key('End');
    assert.equal(env.items.at(-1).options.preventScroll, true);
    env.click(second);
    assert.equal(first.attributes['aria-expanded'], 'false');
    assert.match(env.menu.innerHTML, /owner2@example.com/);
    env.key('Escape');
    assert.equal(env.menu.hidden, true);
    assert.equal(second.focused.preventScroll, true);
    env.click(second);
    assert.equal(env.menu.hidden, false);
    env.click(second);
    assert.equal(env.menu.hidden, true);
});

test('menu actions use the row that opened the menu and close before dispatch', () => {
    const env = menuHarness();
    env.click(env.trigger('7'));
    const item = {disabled: false, dataset: {action: 'export'}};
    env.click({closest: selector => selector === '#teamRowMenu button[data-action]' ? item : null});
    assert.deepEqual(env.calls, [['7', 'owner7@example.com']]);
    assert.equal(env.menu.hidden, true);
});

function seatsHarness() {
    const elements = new Map(), tabs = [];
    let onClick, available = true, validations = 0;
    const element = id => {
        if (!elements.has(id)) elements.set(id, {innerHTML: '', value: '', focus(options) { this.focused = options; }});
        return elements.get(id);
    };
    const context = vm.createContext({
        document: {getElementById: element, addEventListener(name, fn) { if (name === 'click') onClick = fn; }},
        window: {}, escapeHtml: value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;'),
        canAllocateMemberSeat: () => available,
        selectMemberTab: tab => tabs.push(tab), updateMemberInviteAvailability: () => validations++,
    });
    vm.runInContext(readFileSync('app/static/js/member-seats.js', 'utf8'), context);
    return {element, tabs, seats: context.window.memberSeats,
        get validations() { return validations; },
        setAvailable(value) { available = value; },
        invite(seatType) {
            const invite = {dataset: {seatType}};
            onClick({target: {closest: selector => selector === '#memberPanelSeats .btn-invite-seat' ? invite : null}});
        }};
}

const seatLayout = [{kind: 'premium', label: '高级席位', paid: 2, remaining: 1, seat_type: 'premium',
    slots: [{state: 'joined', label: '<script>@example.com', detail: '已加入'},
        {state: 'available', label: '邀请账号', detail: '空闲席位'}]}];

test('member management renders occupied and empty seats with escaped account data', () => {
    const env = seatsHarness();
    env.seats.update({seat_layout: seatLayout});
    const html = env.element('memberSeatsBoard').innerHTML;
    assert.ok(!html.includes('<script>'));
    assert.match(html, /&lt;script>/);
    assert.equal((html.match(/class="team-seat-slot /g) || []).length, 2);
    assert.match(html, /btn-invite-seat/);
});

test('clicking either seat type switches to invitation without another sync and clears old form values', () => {
    for (const kind of ['default', 'premium']) {
        const env = seatsHarness();
        env.element('memberEmails').value = 'stale@example.com';
        env.element('memberPoolEmail').value = 'stale-pool@example.com';
        env.invite(kind);
        assert.deepEqual(env.tabs, ['invite']);
        assert.equal(env.element('inviteSeatType').value, kind);
        assert.equal(env.element('memberEmails').value, '');
        assert.equal(env.element('memberPoolEmail').value, '');
        assert.equal(env.validations, 1);
        assert.equal(env.element('memberEmails').focused.preventScroll, true);
    }
});

test('unknown and exhausted seats cannot start an invitation', () => {
    const env = seatsHarness();
    env.invite('unknown');
    env.setAvailable(false);
    env.invite('premium');
    assert.deepEqual(env.tabs, []);
});

test('refresh and partial batches remove stale invite cards until the full snapshot arrives', () => {
    const env = seatsHarness();
    env.seats.update({seat_layout: seatLayout});
    env.seats.reset();
    assert.doesNotMatch(env.element('memberSeatsBoard').innerHTML, /btn-invite-seat/);
    env.seats.update({pagination: {has_more: true}, seat_layout: seatLayout});
    assert.doesNotMatch(env.element('memberSeatsBoard').innerHTML, /btn-invite-seat/);
    env.seats.update({pagination: {has_more: false}, seat_layout: seatLayout});
    assert.match(env.element('memberSeatsBoard').innerHTML, /btn-invite-seat/);
    env.seats.fail('<error>');
    assert.match(env.element('memberSeatsBoard').innerHTML, /席位同步失败/);
    assert.doesNotMatch(env.element('memberSeatsBoard').innerHTML, /<error>/);
});

test('opening Team member management selects all seats while synchronizing', async () => {
    const env = memberHarness();
    env.context.closeMemberAuthorization = () => {};
    env.context.loadModalMemberList = async () => {};
    await env.context.viewMembers(1, 'owner@example.com');
    assert.equal(env.element('memberPanelSeats').hidden, false);
    assert.equal(env.element('memberPanelJoined').hidden, true);
    assert.equal(env.element('memberPanelInvite').hidden, true);
});

test('standard seat previews show only Team expiry and pending activation', () => {
    const env = seatsHarness();
    env.seats.update({seat_layout: [{kind: 'standard', label: '标准席位', paid: 4, remaining: 1, seat_type: 'default', slots: [
        {state: 'joined', label: 'protected@example.com', detail: '已加入', seat_status: 'warranty', status_label: '质保中',
            status_reason: '免小时级及额度清退，Team 到期后下线', server_now: '2026-10-24T12:00:00',
            warranty_expires_at: '2026-11-23T12:00:00', auto_kick_at: '2026-11-10T12:00:00', team_expires_at: '2026-11-10T12:00:00'},
        {state: 'invited', label: 'pending@example.com', seat_status: 'invited', status_label: '等待确认邀请',
            status_reason: '确认加入后开始计时', team_expires_at: '2026-11-10T12:00:00'},
        {state: 'reserved', label: '已占用 / 预留', detail: '待同步确认'},
        {state: 'available', label: '邀请账号', detail: '空闲席位'},
    ]}]});
    const html = env.element('memberSeatsBoard').innerHTML;
    assert.match(html, />使用中<\/span>/);
    assert.doesNotMatch(html, /质保剩余|质保截止|加入时间|退出时间|Team 到期下线/);
    assert.match(html, /到期 2026-11-10 12:00/);
    assert.match(html, />待确认<\/span>/);
    assert.doesNotMatch(html, /质保中|保留成员|免小时级|额度清退|确认加入后开始计时|team-seat-slot-detail/);
    assert.match(html, /未邀请/);
    assert.doesNotMatch(html, /NaN/);
    assert.equal((html.match(/btn-invite-seat/g) || []).length, 1);
});

test('premium seat previews include join and exit times and opt-out has no stale exit date', () => {
    const env = seatsHarness();
    env.seats.update({seat_layout: [{kind: 'premium', label: '高级席位', paid: 2, remaining: 0, slots: [
        {state: 'joined', label: 'timed@example.com', seat_status: 'timed', status_label: '定时使用中',
            status_reason: '按使用时长下线', server_now: '2026-10-24T12:00:00',
            joined_at: '2026-10-24T12:00:00', auto_kick_at: '2026-10-24T14:00:00'},
        {state: 'joined', label: 'retained@example.com', seat_status: 'retained', status_label: '保留成员',
            status_reason: '不参与自动下线', joined_at: '2026-10-23T10:00:00', auto_kick_enabled: false,
            auto_kick_at: '2026-10-24T15:00:00'},
    ]}]});
    const html = env.element('memberSeatsBoard').innerHTML;
    assert.match(html, /加入 2026-10-24 12:00/);
    assert.match(html, /退出 2026-10-24 14:00/);
    assert.match(html, /加入 2026-10-23 10:00/);
    assert.match(html, />不自动下线<\/span>/);
    assert.match(html, /退出 —/);
    assert.doesNotMatch(html, /退出 2026-10-24 15:00|保留成员|本轮剩余|按使用时长下线/);
});

test('standard warranty and legacy retention labels all render as a single occupied state', () => {
    const env = seatsHarness();
    const states = ['warranty', 'protected', 'retained', 'timed'];
    env.seats.update({seat_layout: [{kind: 'standard', label: '标准席位', paid: 4, remaining: 0, slots:
        states.map(seat_status => ({state: 'joined', label: `${seat_status}@example.com`, seat_status,
            status_label: '旧权益状态', status_reason: '旧规则说明', auto_kick_enabled: false,
            warranty_expires_at: '2026-10-23T12:00:00', team_expires_at: '2026-11-10T12:00:00',
            auto_kick_at: '2026-10-23T12:00:00', server_now: '2026-10-24T12:00:00'})),
    }]});
    const html = env.element('memberSeatsBoard').innerHTML;
    assert.equal((html.match(/>使用中<\/span>/g) || []).length, 4);
    assert.doesNotMatch(html, /旧权益状态|旧规则说明|不自动下线|待下线/);
});

test('card status reflects departure deadline instead of warranty expiry', () => {
    const env = seatsHarness();
    env.seats.update({seat_layout: [{kind: 'premium', label: '高级席位', paid: 2, remaining: 0, seat_type: 'premium', slots: [
        {state: 'joined', label: 'timed@example.com', seat_status: 'timed', status_label: '定时使用中',
            status_reason: '按使用时长下线', server_now: '2026-10-24T12:00:00', auto_kick_at: '2026-10-24T14:00:00'},
        {state: 'joined', label: 'old@example.com', seat_status: 'warranty', status_label: '质保中',
            status_reason: 'Team 到期后下线', server_now: '2026-10-24T12:00:00',
            auto_kick_at: '2026-10-24T11:00:00', warranty_expires_at: '2026-10-23T12:00:00'},
    ]}]});
    const html = env.element('memberSeatsBoard').innerHTML;
    assert.match(html, />使用中<\/span>/);
    assert.match(html, />待下线<\/span>/);
    assert.doesNotMatch(html, /质保|本轮剩余|定时使用中|按使用时长下线/);
});

test('owner, pending invitation and incomplete seat cards keep concise special markers', () => {
    const env = seatsHarness();
    env.seats.update({seat_layout: [{kind: 'standard', seat_type: 'default', label: '标准席位', paid: 3, remaining: 0, slots: [
        {state: 'joined', label: 'owner@example.com', seat_status: 'owner',
            auto_kick_at: '2026-10-23T12:00:00', server_now: '2026-10-24T12:00:00'},
        {state: 'invited', label: 'pending@example.com', team_expires_at: '2026-11-10T12:00:00'},
        {state: 'reserved', label: '已占用 / 预留', detail: '待同步确认'},
    ]}]});
    const html = env.element('memberSeatsBoard').innerHTML;
    for (const label of ['所有者', '待确认', '待同步']) assert.ok(html.includes(`>${label}</span>`));
    assert.doesNotMatch(html, /2026-10-23|NaN|undefined|待同步确认/);
});

test('Team action menu no longer exposes warranty routing as a seat entitlement switch', () => {
    const env = menuHarness();
    env.click(env.trigger('1'));
    assert.doesNotMatch(env.menu.innerHTML, /质保车位|data-action="warranty"/);
});
