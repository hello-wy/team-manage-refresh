const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {randomUUID} = require('node:crypto');
const {mkdtemp, rm} = require('node:fs/promises');
const {createServer} = require('node:net');
const {tmpdir} = require('node:os');
const {join, resolve} = require('node:path');
const {chromium} = require('playwright');

const ROOT = resolve(__dirname, '../..');
const VIEWPORT = {width: 1600, height: 1000};
const MOBILE_VIEWPORT = {width: 390, height: 844};
const TEST_TIMEOUT_MS = 15_000;

async function availablePort() {
    const server = createServer();
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const port = server.address().port;
    await new Promise(resolve => server.close(resolve));
    return port;
}

async function startServer() {
    const directory = await mkdtemp(join(tmpdir(), 'team-workspace-'));
    const port = await availablePort();
    const password = randomUUID();
    const env = {
        ...process.env, DATABASE_URL: `sqlite+aiosqlite:///${join(directory, 'test.db')}`,
        ADMIN_PASSWORD: password, SESSION_SECRET_KEY: randomUUID(), ENCRYPTION_KEY: randomUUID(),
        PYTHONPATH: `${ROOT}:${join(ROOT, 'tests/helpers')}`,
    };
    const server = spawn(process.env.WORKSPACE_TEST_PYTHON, [
        '-m', 'uvicorn', 'workspace_browser_server:app', '--host', '127.0.0.1',
        '--port', String(port), '--lifespan', 'off',
    ], {cwd: ROOT, env, stdio: ['ignore', 'pipe', 'pipe']});
    await new Promise((resolve, reject) => {
        let logs = '';
        const output = chunk => {
            logs += chunk;
            if (logs.includes('Uvicorn running')) resolve();
        };
        server.stdout.on('data', output);
        server.stderr.on('data', output);
        server.once('error', reject);
        server.once('exit', code => reject(new Error(`测试服务器退出：${code}\n${logs}`)));
    });
    return {server, directory, password, base: `http://127.0.0.1:${port}`};
}

async function ready(page, selector) {
    await page.waitForFunction(() => {
        const frame = document.getElementById('adminWorkspaceFrame');
        return frame.contentWindow.location.href === window.location.href
            && frame.contentDocument.readyState === 'complete'
            && document.getElementById('adminWorkspace').getAttribute('aria-busy') !== 'true';
    });
    const frame = page.frameLocator('#adminWorkspaceFrame');
    await frame.locator(selector).waitFor();
    return frame;
}

async function navigate({page, base}, path, selector) {
    await page.locator(`.desktop-nav-link[href="${path}"]`).click();
    await page.waitForURL(`${base}${path === '/admin' ? '/admin/' : path}`);
    return ready(page, selector);
}

async function assertPersistent(page) {
    assert.equal(await page.evaluate(() => window.savedHeader === document.querySelector('.navbar')
        && window.savedIcon === document.querySelector('link[rel="icon"]')
        && window.savedLogo === document.querySelector('.brand-mark-image')), true);
}

async function checkTabs(test) {
    const pages = [
        ['/admin/account-pool', '.account-pool-page'],
        ['/admin/phone-verification', '.phone-workspace'],
        ['/admin/codes', '.codes-workspace-page'],
        ['/admin/official-keys', '.official-keys-page'],
        ['/admin/settings', '.settings-workspace-page'],
        ['/admin', '.team-dashboard-page'],
        ['/admin/codes', '.codes-workspace-page'],
    ];
    for (const [path, selector] of pages) {
        await navigate(test, path, selector);
        await assertPersistent(test.page);
    }
}

async function checkTableHistory({page, base}) {
    let frame = page.frameLocator('#adminWorkspaceFrame');
    await frame.locator('.pagination a').filter({hasText: '2'}).first().click();
    await page.waitForURL(/page=2/);
    assert.equal(await frame.locator('.data-table tbody tr').count(), 10);
    await page.goBack();
    await page.waitForURL(`${base}/admin/codes`);
    frame = await ready(page, '.codes-workspace-page');
    assert.equal(await frame.locator('.data-table tbody tr').count(), 50);
    await page.goForward();
    await page.waitForURL(/page=2/);
    frame = await ready(page, '.codes-workspace-page');
    assert.equal(await frame.locator('.data-table tbody tr').count(), 10);
    const input = frame.locator('.codes-search-form input[name=search]');
    await input.fill('WORKSPACE-TEST-001');
    await input.press('Enter');
    await page.waitForURL(/search=WORKSPACE-TEST-001/);
    assert.equal(await frame.locator('.data-table tbody tr').count(), 1);
}

async function checkModalsAndTheme(page) {
    const frame = page.frameLocator('#adminWorkspaceFrame');
    await frame.locator('.page-header').getByRole('button', {name: '生成兑换码', exact: true}).click();
    await frame.locator('#generateCodeModal.show').waitFor();
    await frame.locator('#generateCodeModal .modal-close').click();
    await page.locator('#openRenewalRequestsBtn').click();
    await page.locator('.renewal-task-card').waitFor();
    assert.match(await page.locator('.renewal-task-card').textContent(), /workspace@example.com/);
    await page.locator('#renewalRequestsModal .modal-close').click();
    const theme = await page.locator('body').getAttribute('data-ui-theme');
    await page.locator('#openThemeSwitcherBtn').click();
    await page.waitForFunction(old => document.body.dataset.uiTheme !== old, theme);
    await frame.locator('body').evaluate(old => new Promise(resolve => {
        const observer = new MutationObserver(check);
        function check() {
            if (document.body.dataset.uiTheme === old) return;
            observer.disconnect();
            resolve();
        }
        observer.observe(document.body, {attributes: true});
        check();
    }), theme);
    await assertPersistent(page);
}

async function checkMobileAndLogin({page, base}) {
    await page.setViewportSize(MOBILE_VIEWPORT);
    await page.locator('.navbar .mobile-menu-toggle').click();
    await page.locator('#adminSidebar.open').waitFor();
    await page.locator('#adminSidebar a[href="/admin/account-pool"]').click();
    await page.waitForURL(`${base}/admin/account-pool`);
    await ready(page, '.account-pool-page');
    assert.equal(await page.locator('#adminSidebar').evaluate(node => node.classList.contains('open')), false);
    await assertPersistent(page);
    assert.equal(await page.locator('#adminSidebar').evaluate(node => getComputedStyle(node).boxShadow), 'none');
    await page.request.post(`${base}/auth/logout`);
    await page.locator('.navbar .mobile-menu-toggle').click();
    await page.locator('#adminSidebar a[href="/admin/codes"]').click();
    await page.waitForURL(`${base}/login`);
    assert.equal(await page.locator('#loginForm').count(), 1);
}

async function verify(test, browser) {
    const page = await browser.newPage({viewport: VIEWPORT});
    page.setDefaultTimeout(TEST_TIMEOUT_MS);
    const errors = [], topNavigations = [];
    page.on('pageerror', error => errors.push(error.message));
    const login = await page.request.post(`${test.base}/auth/login`, {data: {password: test.password}});
    assert.equal(login.status(), 200);
    await page.goto(`${test.base}/admin/`);
    await ready(page, '.team-dashboard-page');
    await page.evaluate(() => {
        window.savedHeader = document.querySelector('.navbar');
        window.savedIcon = document.querySelector('link[rel="icon"]');
        window.savedLogo = document.querySelector('.brand-mark-image');
    });
    page.on('request', request => {
        if (request.isNavigationRequest() && request.frame() === page.mainFrame()) topNavigations.push(request.url());
    });
    const context = {page, base: test.base};
    await checkTabs(context);
    await checkTableHistory(context);
    await checkModalsAndTheme(page);
    assert.deepEqual(topNavigations, []);
    await checkMobileAndLogin(context);
    assert.deepEqual(errors, []);
    console.log('PASS: tabs, persistent header/logo/favicon, history, search, modals, theme, mobile and login expiry');
}

async function main() {
    assert.ok(process.env.WORKSPACE_TEST_PYTHON, 'Set WORKSPACE_TEST_PYTHON to the project Python runtime');
    assert.ok(process.env.WORKSPACE_TEST_CHROME, 'Set WORKSPACE_TEST_CHROME to the browser executable');
    const test = await startServer();
    let browser;
    try {
        browser = await chromium.launch({headless: true, executablePath: process.env.WORKSPACE_TEST_CHROME});
        await verify(test, browser);
    } finally {
        if (browser) await browser.close();
        const exited = new Promise(resolve => test.server.once('exit', resolve));
        test.server.kill('SIGTERM');
        await exited;
        await rm(test.directory, {recursive: true});
    }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
