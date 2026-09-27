const accountPoolSelection = new Set();
const ACCOUNT_POOL_POLL_MS = 2000;
const ACCOUNT_POOL_ROTATION_KEY = 'account_pool_rotation_batches_v2';
const accountPoolRotationResults = new Map();
const accountPoolRotationNotes = [];
const accountPoolRotationWatchers = new Set();
let accountPoolHistoryPage = 1;

function selectedAccountPoolIds() {
    return [...accountPoolSelection];
}

function updateAccountPoolSelectionUi() {
    const count = accountPoolSelection.size;
    const bar = document.getElementById('accountPoolBatchBar');
    if (bar) bar.hidden = count === 0;
    document.getElementById('accountPoolSelectedCount').textContent = `已选择 ${count} 个账号`;
    for (const id of ['accountPoolBatchJson', 'accountPoolBatchSub2api',
        'accountPoolBatchRotate', 'accountPoolBatchDelete', 'accountPoolBatchClear']) {
        document.getElementById(id).disabled = count === 0;
    }
    const boxes = [...document.querySelectorAll('.account-pool-row-select:not(:disabled)')];
    const selectPage = document.getElementById('accountPoolSelectPage');
    if (selectPage) {
        const checked = boxes.filter(box => box.checked).length;
        selectPage.checked = boxes.length > 0 && checked === boxes.length;
        selectPage.indeterminate = checked > 0 && checked < boxes.length;
    }
}

window.clearAccountPoolSelection = function clearAccountPoolSelection() {
    accountPoolSelection.clear();
    document.querySelectorAll('.account-pool-row-select:not(:disabled)').forEach(box => { box.checked = false; });
    updateAccountPoolSelectionUi();
};

window.initAccountPoolBatchSelection = function initAccountPoolBatchSelection() {
    document.querySelectorAll('.account-pool-row-select:disabled').forEach(box => accountPoolSelection.delete(Number(box.value)));
    document.querySelectorAll('.account-pool-row-select:not(:disabled)').forEach(box => {
        box.checked = accountPoolSelection.has(Number(box.value));
        box.addEventListener('change', () => {
            const id = Number(box.value);
            if (box.checked) accountPoolSelection.add(id);
            else accountPoolSelection.delete(id);
            updateAccountPoolSelectionUi();
        });
    });
    document.getElementById('accountPoolSelectPage')?.addEventListener('change', event => {
        document.querySelectorAll('.account-pool-row-select:not(:disabled)').forEach(box => {
            box.checked = event.target.checked;
            const id = Number(box.value);
            if (box.checked) accountPoolSelection.add(id);
            else accountPoolSelection.delete(id);
        });
        updateAccountPoolSelectionUi();
    });
    updateAccountPoolSelectionUi();
};

function showAccountPoolBatchProgress(lines) {
    const panel = document.getElementById('accountPoolBatchProgress');
    panel.hidden = false;
    panel.textContent = lines.join('\n');
}

async function requestAccountPoolBatch(action, ids) {
    const response = await fetch(`/admin/account-pool/batch/${action}`, {
        method: 'POST', credentials: 'same-origin', cache: 'no-store',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ids}),
    });
    if (!response.ok) {
        const result = await response.json();
        throw new Error(result.error || '批量操作失败');
    }
    return response;
}

async function downloadAccountPoolBatchJson() {
    const button = document.getElementById('accountPoolBatchJson');
    button.disabled = true;
    try {
        const response = await requestAccountPoolBatch('export-json', selectedAccountPoolIds());
        downloadAccountPoolJson(await response.blob(), 'batch');
        showToast('合并 JSON 已下载', 'success');
    } catch (error) {
        showToast(error.message, 'error');
    } finally {
        updateAccountPoolSelectionUi();
    }
}

async function watchAccountPoolBatchExports(jobIds) {
    const results = await Promise.all(jobIds.map(async id => {
        const response = await fetch(`/admin/account-pool/export-jobs/${id}`, {
            credentials: 'same-origin', cache: 'no-store',
        });
        if (!response.ok) throw new Error(`导出任务 ${id} 状态读取失败`);
        return {id, ...await response.json()};
    }));
    const done = results.filter(item => item.status === 'completed').length;
    const failed = results.filter(item => item.status === 'failed');
    showAccountPoolBatchProgress([
        `Sub2API 导出：成功 ${done}，失败 ${failed.length}，进行中 ${results.length - done - failed.length}`,
        ...failed.map(item => `任务 #${item.id}：${item.error}`),
    ]);
    if (done + failed.length === results.length) {
        await refreshAccountPoolTable();
        return;
    }
    setTimeout(() => watchAccountPoolBatchExports(jobIds).catch(showAccountPoolBatchError), ACCOUNT_POOL_POLL_MS);
}

async function exportAccountPoolBatchSub2api() {
    if (!await confirmAccountPoolAction(`确定将选中的 ${accountPoolSelection.size} 个账号导出到 Sub2API 吗？`)) return;
    try {
        const response = await requestAccountPoolBatch('sub2api', selectedAccountPoolIds());
        const result = await response.json();
        showAccountPoolBatchProgress([`已提交 ${result.job_ids.length} 个 Sub2API 导出任务`]);
        await watchAccountPoolBatchExports(result.job_ids);
    } catch (error) {
        showAccountPoolBatchError(error);
    }
}

function showAccountPoolBatchError(error) {
    showToast(error.message || '批量任务失败', 'error');
    showAccountPoolBatchProgress([error.message || '批量任务失败']);
}

function savedAccountPoolRotationBatches() {
    try {
        const saved = JSON.parse(sessionStorage.getItem(ACCOUNT_POOL_ROTATION_KEY) || '[]');
        return new Set(Array.isArray(saved) ? saved.filter(id => typeof id === 'string') : []);
    } catch {
        return new Set();
    }
}

function renderAccountPoolRotationProgress() {
    const results = [...accountPoolRotationResults.values()].flat();
    const completed = results.filter(item => item.status === 'completed').length;
    const failed = results.filter(item => item.status === 'failed').length;
    const pending = results.length - completed - failed;
    showAccountPoolBatchProgress([
        `2FA 更换：成功 ${completed}，失败 ${failed}，进行中 ${pending}`,
        ...accountPoolRotationNotes,
        ...results.map(item => {
            const status = {pending: '等待', running: '执行中', completed: '成功', failed: '失败'}[item.status] || item.status;
            return `${item.email}：${status}${item.error ? ` · ${item.error}` : ''}` +
                (item.two_factor_secret ? ` · 新密钥 ${item.two_factor_secret}` : '');
        }),
    ]);
}

function startAccountPoolRotationWatch(batchId, notes = []) {
    accountPoolRotationNotes.push(...notes);
    if (!batchId) {
        renderAccountPoolRotationProgress();
        return;
    }
    if (accountPoolRotationWatchers.has(batchId)) return;
    const saved = savedAccountPoolRotationBatches();
    saved.add(batchId);
    sessionStorage.setItem(ACCOUNT_POOL_ROTATION_KEY, JSON.stringify([...saved]));
    accountPoolRotationWatchers.add(batchId);
    showAccountPoolBatchProgress(['2FA 更换任务已启动，正在读取进度...', ...accountPoolRotationNotes]);
    watchAccountPoolBatchRotations(batchId).catch(error => handleAccountPoolRotationError(batchId, error));
}

function handleAccountPoolRotationError(batchId, error) {
    accountPoolRotationWatchers.delete(batchId);
    showAccountPoolBatchError(new Error(`${error.message}；后台任务会继续，可刷新页面或查看“2FA 记录”。`));
}

async function watchAccountPoolBatchRotations(batchId) {
    const response = await fetch(`/admin/account-pool/batch/rotate-2fa/${batchId}`, {
        credentials: 'same-origin', cache: 'no-store',
    });
    if (!response.ok) throw new Error('读取 2FA 任务结果失败');
    const {results} = await response.json();
    accountPoolRotationResults.set(batchId, results);
    results.filter(item => ['completed', 'failed'].includes(item.status))
        .forEach(item => {
            window.accountPoolCredentialStore.clear(item.email);
            if (item.status === 'completed' && item.two_factor_secret) {
                document.querySelectorAll('totp-code[data-email]').forEach(control => {
                    if (control.dataset.email === item.email) control.secret = item.two_factor_secret;
                });
            }
        });
    renderAccountPoolRotationProgress();
    if (results.every(item => ['completed', 'failed'].includes(item.status))) {
        const saved = savedAccountPoolRotationBatches();
        saved.delete(batchId);
        sessionStorage.setItem(ACCOUNT_POOL_ROTATION_KEY, JSON.stringify([...saved]));
        accountPoolRotationWatchers.delete(batchId);
        await refreshAccountPoolTable();
        return;
    }
    setTimeout(() => watchAccountPoolBatchRotations(batchId).catch(error => handleAccountPoolRotationError(batchId, error)), ACCOUNT_POOL_POLL_MS);
}

async function rotateAccountPoolBatch() {
    if (!await confirmAccountPoolAction(`确定更换选中的 ${accountPoolSelection.size} 个账号的 2FA 吗？旧密钥会失效。`)) return;
    try {
        const response = await requestAccountPoolBatch('rotate-2fa', selectedAccountPoolIds());
        const {batch_id: batchId} = await response.json();
        startAccountPoolRotationWatch(batchId);
    } catch (error) {
        showAccountPoolBatchError(error);
    }
}

async function deleteAccountPoolBatch() {
    if (!await confirmAccountPoolAction(`确定永久删除选中的 ${accountPoolSelection.size} 个账号及加入历史吗？此操作无法恢复。`)) return;
    try {
        const response = await requestAccountPoolBatch('delete', selectedAccountPoolIds());
        const {deleted} = await response.json();
        accountPoolSelection.clear();
        showToast(`已删除 ${deleted.length} 个账号`, 'success');
        await refreshAccountPoolTable();
        updateAccountPoolSelectionUi();
    } catch (error) {
        showAccountPoolBatchError(error);
    }
}

async function loadAccountPoolRotationHistory(page) {
    const container = document.getElementById('accountPoolBatchHistoryResults');
    container.textContent = '加载中...';
    try {
        const response = await fetch(`/admin/account-pool/batch/rotate-2fa-history?page=${page}`, {
            credentials: 'same-origin', cache: 'no-store',
        });
        if (!response.ok) throw new Error('读取 2FA 记录失败');
        const data = await response.json();
        accountPoolHistoryPage = page;
        container.replaceChildren();
        if (!data.results.length) container.textContent = '暂无记录';
        data.results.forEach(item => {
            const row = document.createElement('div');
            row.className = 'account-pool-batch-history-item';
            const status = {pending: '等待', running: '执行中', completed: '成功', failed: '失败'}[item.status];
            row.textContent = `${accountPoolFormatDate(item.created_at)} · ${item.email} · ${status}` +
                (item.error ? `\n${item.error}` : '') +
                (item.two_factor_secret ? `\n新密钥：${item.two_factor_secret}` : '');
            container.appendChild(row);
        });
        const totalPages = Math.max(1, Math.ceil(data.total / data.page_size));
        document.getElementById('accountPoolHistoryPage').textContent =
            `第 ${page} / ${totalPages} 页，共 ${data.total} 条`;
        document.getElementById('accountPoolHistoryPrev').disabled = page <= 1;
        document.getElementById('accountPoolHistoryNext').disabled = page >= totalPages;
    } catch (error) {
        container.textContent = error.message;
        showToast(error.message, 'error');
    }
}

document.addEventListener('DOMContentLoaded', () => {
    window.initAccountPoolBatchSelection();
    document.getElementById('accountPoolBatchJson').addEventListener('click', downloadAccountPoolBatchJson);
    document.getElementById('accountPoolBatchSub2api').addEventListener('click', exportAccountPoolBatchSub2api);
    document.getElementById('accountPoolBatchRotate').addEventListener('click', rotateAccountPoolBatch);
    document.getElementById('accountPoolBatchDelete').addEventListener('click', deleteAccountPoolBatch);
    document.getElementById('accountPoolBatchHistory').addEventListener('click', () => {
        showModal('accountPoolBatchHistoryModal');
        loadAccountPoolRotationHistory(1);
    });
    document.getElementById('accountPoolHistoryPrev').addEventListener('click', () => {
        loadAccountPoolRotationHistory(accountPoolHistoryPage - 1);
    });
    document.getElementById('accountPoolHistoryNext').addEventListener('click', () => {
        loadAccountPoolRotationHistory(accountPoolHistoryPage + 1);
    });
    document.getElementById('accountPoolBatchClear').addEventListener('click', () => {
        window.clearAccountPoolSelection();
    });
    const saved = savedAccountPoolRotationBatches();
    const legacyBatch = sessionStorage.getItem('account_pool_rotation_batch');
    if (legacyBatch) {
        saved.add(legacyBatch);
        sessionStorage.removeItem('account_pool_rotation_batch');
    }
    saved.forEach(batchId => startAccountPoolRotationWatch(batchId));
});
