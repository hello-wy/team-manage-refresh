const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const vm = require('node:vm');

const field = value => ({value, disabled: false});
const button = textContent => ({textContent, disabled: false});

function importForm(buttons) {
    return {
        accessToken: field('stale-at'), idToken: field('stale-id'),
        refreshToken: field('stale-rt'), sessionToken: field('stale-session'),
        clientId: field('app_test'), email: field('stale@example.com'),
        accountId: field('stale-account'), loginEmail: field('owner@example.com'),
        loginPassword: field('owner-password'), loginTotp: field('JBSWY3DPEHPK3PXP'),
        loginAccountId: field('owner-team'), dataset: {}, resetCount: 0,
        querySelectorAll: () => buttons,
        reset() { this.resetCount += 1; },
    };
}

function importSection(submit, fields) {
    return {
        style: {display: 'block'},
        querySelector: () => submit,
        querySelectorAll: () => fields,
    };
}

function loadImportScripts(context) {
    for (const file of ['main.js', 'team-import.js']) {
        vm.runInContext(readFileSync(join(__dirname, '../../app/static/js', file), 'utf8'), context);
    }
}

function harness() {
    const quickButton = button('导入'), manualButton = button('导入 Team'), credentialButton = button('登录并导入 Team');
    const buttons = [quickButton, manualButton, credentialButton, button('获取授权链接'), button('切换模式')];
    const form = importForm(buttons);
    const callback = field('http://localhost:1455/auth/callback?code=first&state=test-state');
    const quick = importSection(quickButton, [callback]);
    const manual = importSection(manualButton, [form.accessToken, form.idToken, form.refreshToken,
        form.sessionToken, form.clientId, form.email, form.accountId]);
    const credentials = importSection(credentialButton, [form.loginEmail, form.loginPassword, form.loginTotp, form.loginAccountId]);
    credentials.style.display = 'none';
    const elements = {singleImportForm: form, oauthCallbackInput: callback,
        oauthQuickSection: quick, manualTokenSection: manual, teamCredentialLoginSection: credentials,
        oauthAuthorizeUrlOutput: field('')};
    const calls = [], toasts = [];
    const document = {addEventListener() {}, getElementById: id => elements[id], body: {dataset: {poolType: 'welfare'}}};
    const context = vm.createContext({document, window: {}, console});
    loadImportScripts(context);
    context.showToast = (...args) => toasts.push(args);
    context.getFriendlyAdminErrorMessage = value => value;
    context.refreshVisibleAdminData = async () => { context.refreshCount += 1; };
    context.refreshCount = 0;
    const parsed = {access_token: 'new-at', refresh_token: 'new-rt', id_token: 'new-id',
        client_id: 'app_new', raw: {email: 'new@example.com', account_id: 'new-account'}};
    context.parseResult = {success: true, data: {success: true, data: parsed}};
    context.importResult = {success: true, data: {success: true}};
    context.apiCall = async (url, options) => {
        calls.push({url, body: JSON.parse(options.body)});
        return url.endsWith('parse-callback') ? context.parseResult : context.importResult;
    };
    context.setSingleImportMode('quick');
    vm.runInContext("oauthDraft = {codeVerifier:'test-verifier', state:'test-state', clientId:'app_test'}", context);
    return {context, form, callback, buttons, quickButton, manualButton, credentialButton, calls, toasts,
        submit: () => context.handleSingleImport({target: form, preventDefault() {}})};
}

module.exports = {harness};
