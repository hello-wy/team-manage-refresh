const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const vm = require('node:vm');

const source = readFileSync(join(__dirname, '../../app/static/js/main.js'), 'utf8');
function section(start, end) {
    return source.slice(source.indexOf(start), source.indexOf(end));
}

function harness() {
    const elements = new Map();
    const element = id => {
        if (!elements.has(id)) {
            const classes = new Set();
            elements.set(id, {
                value: '', textContent: '', innerHTML: '', disabled: false, attributes: {},
                setAttribute(key, value) { this.attributes[key] = String(value); },
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
        loadMemberPoolOptions: async () => {},
        renderMemberSeatSummary: () => '席位余额',
        renderMemberSeatControl: () => '',
        renderMemberSeatType: () => '',
        renderMemberAutoKickTime: () => '',
        renderMemberAutoKickControl: () => '',
        renderMemberAuthorizationActions: () => '',
        renderMemberAuthorizationStatus: () => '',
        memberAccountCredentials: {reset() {}},
        escapeHtml: value => String(value),
        formatDateTime: () => '',
        getFriendlyAdminErrorMessage: value => value,
        apiCall: async url => { calls.push(url); return context.response; },
    });
    vm.runInContext(`
        let memberListRequestId = 0, memberSeatBalance = null, memberExistingEmails = new Set(), renderedMemberTeamId = null;
        ${section('let memberAuthorizationContext = null;', 'function generateMemberAuthorization()')}
        ${section('async function viewMembers(', 'async function revokeInvite(')}
        ${readFileSync(join(__dirname, '../../app/static/js/invited-member-credentials.js'), 'utf8')}
        ${readFileSync(join(__dirname, '../../app/static/js/member-list.js'), 'utf8')}
        memberAuthorizationContext = {teamId: 1, email: 'member@example.com', busy: false, hasLink: true};
    `, context);
    return {context, element, calls, toasts, timers,
        run: expression => vm.runInContext(expression, context)};
}

module.exports = {harness, section};
