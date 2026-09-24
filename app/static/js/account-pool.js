function accountPoolFormatDate(value) {
    if (!value) return '-';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return date.toLocaleString('zh-CN', {
        year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit'
    });
}

async function copyAccountPoolCredential(email, field, label) {
    try {
        const credentials = await window.accountPoolCredentialStore.load(email);
        const value = credentials[field];
        if (!value) throw new Error(`该账号未保存${label}`);
        await copyToClipboard(value);
    } catch (error) {
        showToast(error.message || `复制${label}失败`, 'error');
    }
}

async function openAccountPoolCredentialEditor(entryId, email) {
    const form = document.getElementById('accountPoolCredentialForm');
    const passwordInput = document.getElementById('accountPoolCredentialPassword');
    const secretInput = document.getElementById('accountPoolCredentialTwoFactor');
    form.dataset.entryId = String(entryId);
    document.getElementById('accountPoolCredentialEmail').textContent = email;
    passwordInput.value = '';
    secretInput.value = '';
    showModal('accountPoolCredentialModal');
    try {
        const credentials = await window.accountPoolCredentialStore.load(email, {fresh: true});
        passwordInput.value = credentials.password || '';
        secretInput.value = credentials.two_factor_secret || '';
        passwordInput.focus();
    } catch (error) {
        hideModal('accountPoolCredentialModal');
        showToast(error.message || '读取账号凭据失败', 'error');
    }
}

async function submitAccountPoolCredentialForm(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const button = document.getElementById('accountPoolCredentialSave');
    const password = document.getElementById('accountPoolCredentialPassword').value;
    const twoFactorSecret = document.getElementById('accountPoolCredentialTwoFactor').value.trim();
    if (!password && !twoFactorSecret) return showToast('请至少填写密码或 2FA', 'warning');
    const payload = {};
    if (password) payload.password = password;
    if (twoFactorSecret) payload.two_factor_secret = twoFactorSecret;
    button.disabled = true;
    try {
        const response = await fetch(`/admin/account-pool/${form.dataset.entryId}/credentials`, {
            method: 'PATCH',
            credentials: 'same-origin',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload)
        });
        const result = await response.json();
        if (!response.ok || !result.success) throw new Error(result.error || '保存失败');
        window.accountPoolCredentialStore.clear(result.data.email);
        showToast(result.message, 'success');
        hideModal('accountPoolCredentialModal');
        await refreshAccountPoolTable();
    } catch (error) {
        showToast(error.message || '保存账号凭据失败', 'error');
    } finally {
        button.disabled = false;
    }
}

async function deleteAccountPoolEntry(entryId, email) {
    if (!confirm(`确定永久删除 ${email} 吗？账号信息和加入历史都会从数据库删除，且无法恢复。`)) return;
    try {
        const response = await fetch(`/admin/account-pool/${entryId}`, {
            method: 'DELETE', credentials: 'same-origin'
        });
        const payload = await response.json();
        if (!response.ok || !payload.success) throw new Error(payload.error || '删除失败');
        showToast(payload.message, 'success');
        await refreshAccountPoolTable();
    } catch (error) {
        showToast(error.message || '删除失败', 'error');
    }
}

async function runAccountPoolLiveness() {
    const button = document.getElementById('accountPoolLivenessBtn');
    if (!button) return;
    button.disabled = true;
    button.querySelector('span').textContent = '验活中...';
    try {
        const response = await fetch('/admin/account-pool/liveness', {
            method: 'POST',
            credentials: 'same-origin',
        });
        const payload = await response.json();
        if (!response.ok || !payload.success) throw new Error(payload.error || '验活失败');
        showToast(payload.message, 'success');
        await refreshAccountPoolTable();
    } catch (error) {
        showToast(error.message || '验活失败', 'error');
    } finally {
        button.disabled = false;
        button.querySelector('span').textContent = '立即验活';
    }
}

function accountPoolTeamLabel(team) {
    return `${team.team_name || team.name || `Team #${team.id}`} · ID ${team.id}`;
}

function accountPoolTeamStatusLabel(team) {
    const labels = {active: '可用', full: '已满', expired: '已过期', error: '异常', banned: '已封禁'};
    const status = labels[team.status] || team.status || '未知状态';
    const members = Number.isFinite(Number(team.current_members)) && Number.isFinite(Number(team.max_members))
        ? ` · ${team.current_members}/${team.max_members} 席位`
        : '';
    return `${status}${members}`;
}

function openAccountPoolTeamPicker(entryId, email, teams, action = 'automatic_login', seatType = 'default') {
    const options = document.getElementById('accountPoolTeamPickerOptions');
    document.getElementById('accountPoolTeamPickerEmail').textContent = email;
    const title = document.getElementById('accountPoolTeamPickerTitle');
    if (title) title.textContent = action === 'invite' ? '选择加入的 Team' : '选择目标 Team';
    if (!teams.length) {
        options.innerHTML = '<div class="text-muted">暂无可选择的 Team。</div>';
        showModal('accountPoolTeamPickerModal');
        return;
    }
    options.innerHTML = teams.map(team => `
        <button type="button" class="btn btn-secondary account-pool-team-picker-option" data-team-id="${team.id}">
            <span>${escapeHtml(accountPoolTeamLabel(team))}</span>
            <small>${escapeHtml(team.email || 'Team 邮箱未知')} · ${escapeHtml(accountPoolTeamStatusLabel(team))}</small>
        </button>
    `).join('');
    options.querySelectorAll('[data-team-id]').forEach(button => {
        button.addEventListener('click', () => {
            hideModal('accountPoolTeamPickerModal');
            const teamId = Number(button.dataset.teamId);
            if (action === 'invite') {
                inviteAccountPoolEntry(entryId, email, teamId, seatType, button);
            } else {
                runAccountPoolAutomaticLogin(entryId, email, teamId, button);
            }
        });
    });
    showModal('accountPoolTeamPickerModal');
    if (window.lucide) lucide.createIcons();
}

async function inviteAccountPoolEntry(entryId, email, teamId, seatType, button) {
    if (!confirm(`确定邀请 ${email} 加入所选 Team 吗？`)) return;
    const original = button.innerHTML;
    button.disabled = true;
    button.innerHTML = '<i data-lucide="loader-circle" class="spin" aria-hidden="true"></i>';
    if (window.lucide) lucide.createIcons();
    try {
        const response = await fetch(`/admin/account-pool/${entryId}/invite`, {
            method: 'POST', credentials: 'same-origin',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({team_id: teamId, seat_type: seatType || 'default'})
        });
        const payload = await response.json();
        if (!response.ok || !payload.success) throw new Error(payload.error || payload.message || '邀请失败');
        showToast(payload.message || `${email} 已发送 Team 邀请`, 'success');
        await refreshAccountPoolTable();
    } catch (error) {
        showToast(error.message || '邀请失败', 'error');
        button.disabled = false;
        button.innerHTML = original;
        if (window.lucide) lucide.createIcons();
    }
}

async function openAccountPoolInvitePicker(entryId, email, seatType) {
    document.getElementById('accountPoolTeamPickerOptions').textContent = '正在读取 Team...';
    showModal('accountPoolTeamPickerModal');
    try {
        const response = await fetch('/admin/account-pool/teams', {credentials: 'same-origin'});
        if (!response.ok) throw new Error('读取 Team 失败');
        const payload = await response.json();
        openAccountPoolTeamPicker(entryId, email, payload.teams, 'invite', seatType);
    } catch (error) {
        hideModal('accountPoolTeamPickerModal');
        showToast(error.message, 'error');
    }
}

async function runAccountPoolAutomaticLogin(entryId, email, workspaceId, button) {
    const target = workspaceId ? `Workspace ${workspaceId}` : '当前可用账号';
    const targetHint = workspaceId ? `目标为 ${target}。` : '';
    if (!confirm(`确定重新登录 ${email} 并导入配置的 sub2api 吗？${targetHint}`)) return;
    const original = button.innerHTML;
    button.disabled = true;
    button.innerHTML = '<i data-lucide="loader-circle" class="spin" aria-hidden="true"></i>';
    if (window.lucide) lucide.createIcons();
    try {
        const response = await fetch(`/admin/account-pool/${entryId}/automatic-login`, {
            method: 'POST', credentials: 'same-origin',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({workspace_id: workspaceId || ''})
        });
        const payload = await response.json();
        if (!response.ok || !payload.success) throw new Error(payload.error || '导入 sub2api 失败');
        showToast(`${email} 的 Sub2API 导出任务已开始`, 'success');
        await refreshAccountPoolTable();
        watchAccountPoolExportJob(payload.job_id, email);
    } catch (error) {
        showToast(error.message || '导入 sub2api 失败', 'error');
    } finally {
        button.disabled = false;
        button.innerHTML = original;
        if (window.lucide) lucide.createIcons();
    }
}

const accountPoolWatchedJobs = new Set();

async function watchAccountPoolExportJob(jobId, email) {
    if (accountPoolWatchedJobs.has(jobId)) return;
    accountPoolWatchedJobs.add(jobId);
    try {
        while (true) {
            await new Promise(resolve => setTimeout(resolve, 2000));
            const response = await fetch(`/admin/account-pool/export-jobs/${jobId}`, {
                credentials: 'same-origin', cache: 'no-store'
            });
            if (!response.ok) throw new Error('读取导出任务状态失败');
            const job = await response.json();
            if (job.status === 'pending' || job.status === 'running') continue;
            if (job.status === 'failed') throw new Error(job.error || '导入 Sub2API 失败');
            if (job.status !== 'completed') throw new Error(`未知导出状态：${job.status}`);
            showToast(`${email} 已导入 Sub2API（账户 ID ${job.account_id}）`, 'success');
            await refreshAccountPoolTable();
            break;
        }
    } catch (error) {
        showToast(error.message || '导入 Sub2API 失败', 'error');
        await refreshAccountPoolTable();
    } finally {
        accountPoolWatchedJobs.delete(jobId);
    }
}

async function exportAccountPoolJson(entryId, email, workspaceId, button) {
    if (!confirm(`确定重新登录 ${email} 并导出最新 JSON 吗？`)) return;
    const original = button.innerHTML;
    button.disabled = true;
    button.innerHTML = '<i data-lucide="loader-circle" class="spin" aria-hidden="true"></i>';
    if (window.lucide) lucide.createIcons();
    try {
        const response = await fetch(`/admin/account-pool/${entryId}/export-json`, {
            method: 'POST', credentials: 'same-origin',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({workspace_id: workspaceId || ''})
        });
        if (!response.ok) {
            const payload = await response.json();
            throw new Error(payload.error || '导出 JSON 失败');
        }
        const blob = await response.blob();
        downloadAccountPoolJson(blob, entryId);
        showToast(`${email} 的最新 JSON 已下载`, 'success');
        await refreshAccountPoolTable();
    } catch (error) {
        showToast(error.message || '导出 JSON 失败', 'error');
    } finally {
        button.disabled = false;
        button.innerHTML = original;
        if (window.lucide) lucide.createIcons();
    }
}

function downloadAccountPoolJson(blob, entryId) {
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `sub2api-account-pool-${entryId}.json`;
    link.click();
    URL.revokeObjectURL(url);
}

async function loadAccountPoolHistory(entryId, email) {
    const content = document.getElementById('accountPoolHistoryContent');
    document.getElementById('accountPoolHistoryEmail').textContent = email;
    content.textContent = '加载中...';
    showModal('accountPoolHistoryModal');
    try {
        const response = await fetch(`/admin/account-pool/${entryId}/history`);
        const payload = await response.json();
        if (!response.ok || !payload.success) throw new Error(payload.error || '加载历史失败');
        renderAccountPoolHistory(content, payload.data.histories || []);
    } catch (error) {
        content.textContent = error.message || '加载历史失败';
    }
}

function renderAccountPoolHistory(content, histories) {
    content.innerHTML = histories.length ? histories.map(history => `
        <div class="account-pool-history-item">
            <div>
                <strong>${escapeHtml(history.team_name || `Team #${history.team_id || '-'}`)}</strong>
                <div class="text-muted small">${escapeHtml(history.team_email || 'Team 邮箱未知')}</div>
            </div>
            <div class="text-muted small">加入：${accountPoolFormatDate(history.joined_at)}<br>离开：${accountPoolFormatDate(history.left_at)}</div>
        </div>
    `).join('') : '<div class="text-muted">暂无加入历史。</div>';
    if (window.lucide) lucide.createIcons();
}

async function submitAccountPoolForm(event) {
    event.preventDefault();
    const input = document.getElementById('accountPoolEmails');
    const button = document.getElementById('accountPoolSubmitBtn');
    const content = input.value.trim();
    if (!content) return showToast('请先输入邮箱', 'warning');
    button.disabled = true;
    try {
        const response = await fetch('/admin/account-pool', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({content})
        });
        const payload = await response.json();
        if (!response.ok || !payload.success) throw new Error(payload.message || payload.error || '添加失败');
        showToast(payload.message, 'success');
        input.value = '';
        await refreshAccountPoolTable();
    } catch (error) {
        showToast(error.message || '添加失败', 'error');
    } finally {
        button.disabled = false;
    }
}

function initAccountPoolColumnToggler() {
    const table = document.querySelector('.account-pool-page .data-table');
    const container = document.getElementById('accountPoolColumnToggleDropdown');
    if (!table || !container) return;

    const storageKey = 'account_pool_list_columns_v2';
    const hiddenColumns = loadAccountPoolHiddenColumns(storageKey);
    container.innerHTML = '<div class="dropdown-header">显示/隐藏列</div>';

    table.querySelectorAll('thead th').forEach((header, index) => {
        const label = header.innerText.trim();
        if (!label || label === '操作' || header.querySelector('input[type="checkbox"]')) return;

        const visible = !hiddenColumns.has(index);
        setAccountPoolColumnVisibility(table, index, visible);

        const item = document.createElement('label');
        item.className = 'dropdown-item';
        item.innerHTML = `<input type="checkbox" ${visible ? 'checked' : ''}><span>${escapeHtml(label)}</span>`;
        item.querySelector('input').addEventListener('change', (event) => {
            const nextVisible = event.target.checked;
            setAccountPoolColumnVisibility(table, index, nextVisible);
            if (nextVisible) hiddenColumns.delete(index);
            else hiddenColumns.add(index);
            localStorage.setItem(storageKey, JSON.stringify([...hiddenColumns].sort((a, b) => a - b)));
        });
        container.appendChild(item);
    });
}

function loadAccountPoolHiddenColumns(storageKey) {
    const stored = localStorage.getItem(storageKey);
    if (!stored) return new Set();

    try {
        const parsed = JSON.parse(stored);
        if (!Array.isArray(parsed)) throw new TypeError('列偏好不是数组');
        return new Set(parsed.filter(Number.isInteger));
    } catch (error) {
        console.error('账号池列设置偏好无效，已重置。', error);
        localStorage.removeItem(storageKey);
        showToast('列设置偏好无效，已重置', 'warning');
        return new Set();
    }
}

function setAccountPoolColumnVisibility(table, index, visible) {
    const hidden = !visible;
    table.querySelectorAll(
        `thead th:nth-child(${index + 1}), tbody tr td:nth-child(${index + 1})`
    ).forEach((cell) => {
        cell.hidden = hidden;
    });
}

function closeAccountPoolColumnDropdown() {
    const menu = document.getElementById('accountPoolColumnToggleDropdown');
    const button = document.getElementById('accountPoolColumnToggleBtn');
    menu?.classList.remove('show');
    button?.setAttribute('aria-expanded', 'false');
}

function toggleAccountPoolColumnDropdown(button) {
    const menu = document.getElementById('accountPoolColumnToggleDropdown');
    if (!menu) return;

    const shouldShow = !menu.classList.contains('show');
    closeFloatingDropdowns();
    button.setAttribute('aria-expanded', 'false');
    if (!shouldShow) return;

    menu.classList.add('show');
    button.setAttribute('aria-expanded', 'true');
    positionFloatingDropdown(menu, button);
    requestAnimationFrame(() => positionFloatingDropdown(menu, button));
}

function initAccountPoolColumnDropdown() {
    const menu = document.getElementById('accountPoolColumnToggleDropdown');
    const button = document.getElementById('accountPoolColumnToggleBtn');
    if (!menu || !button) return;

    button.addEventListener('click', () => toggleAccountPoolColumnDropdown(button));
    document.addEventListener('click', (event) => {
        if (!menu.classList.contains('show')) return;
        if (button.contains(event.target) || menu.contains(event.target)) return;
        closeAccountPoolColumnDropdown();
    });
    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') closeAccountPoolColumnDropdown();
    });
    window.addEventListener('resize', () => {
        if (menu.classList.contains('show')) positionFloatingDropdown(menu, button);
    });
    window.addEventListener('scroll', () => {
        if (menu.classList.contains('show')) positionFloatingDropdown(menu, button);
    }, true);
}

let accountPoolTableRequest = null;

function getAccountPoolTableUrl({page, perPage, statusFilter, search} = {}) {
    const url = new URL('/admin/account-pool', window.location.origin);
    const currentParams = new URLSearchParams(window.location.search);
    const currentSearch = document.querySelector('.account-pool-toolbar-actions .search-form input[name="search"]');
    const currentStatus = document.getElementById('accountPoolStatusFilter');
    const currentPageSize = document.getElementById('accountPoolPageSize');
    url.searchParams.set('page', String(page ?? currentParams.get('page') ?? 1));
    url.searchParams.set('per_page', String(perPage || currentPageSize?.value || currentParams.get('per_page') || 20));
    url.searchParams.set('search', search ?? currentSearch?.value?.trim() ?? '');
    url.searchParams.set('status_filter', statusFilter ?? currentStatus?.value ?? '');
    return url;
}

async function refreshAccountPoolTable(options = {}) {
    const region = document.getElementById('accountPoolTableRegion');
    if (!region) return;
    accountPoolTableRequest?.abort();
    const controller = new AbortController();
    accountPoolTableRequest = controller;
    const url = getAccountPoolTableUrl(options);
    region.classList.add('is-loading');
    try {
        const response = await fetch(url, {
            credentials: 'same-origin',
            headers: {'X-Requested-With': 'XMLHttpRequest'},
            signal: controller.signal,
        });
        if (!response.ok) throw new Error('账号列表加载失败');
        const html = await response.text();
        const documentFragment = new DOMParser().parseFromString(html, 'text/html');
        const nextRegion = documentFragment.getElementById('accountPoolTableRegion');
        if (!nextRegion) throw new Error('账号列表响应格式错误');
        if (controller.signal.aborted) return;
        if (Number(nextRegion.dataset.currentPage) > Number(nextRegion.dataset.totalPages)) {
            await refreshAccountPoolTable({...options, page: Number(nextRegion.dataset.totalPages)});
            return;
        }
        region.replaceWith(nextRegion);
        const count = documentFragment.querySelector('.account-pool-page-header .page-meta-pill');
        const currentCount = document.querySelector('.account-pool-page-header .page-meta-pill');
        if (count && currentCount) currentCount.replaceWith(count);
        window.history.replaceState({}, '', url);
        initAccountPoolRowActions();
        initAccountPoolExportJobs();
        initAccountPoolPageSizeControl();
        initAccountPoolColumnToggler();
        window.initAccountPoolWorkspaceButtons?.();
        window.initAccountPoolRotateButtons?.();
        window.initAccountPoolBatchSelection?.();
        if (window.lucide) lucide.createIcons();
        formatQuotaResetTimes();
    } catch (error) {
        if (error.name !== 'AbortError') showToast(error.message || '账号列表加载失败', 'error');
    } finally {
        if (accountPoolTableRequest === controller) {
            document.getElementById('accountPoolTableRegion')?.classList.remove('is-loading');
        }
    }
}
window.refreshAccountPoolTable = refreshAccountPoolTable;

function initAccountPoolRowActions() {
    document.querySelectorAll('.account-pool-history-btn').forEach(button => {
        if (button.dataset.bound === 'true') return;
        button.dataset.bound = 'true';
        button.addEventListener('click', () => {
            loadAccountPoolHistory(button.dataset.entryId, button.dataset.email);
        });
    });
    document.querySelectorAll('.account-pool-invite-button').forEach(button => {
        if (button.dataset.bound === 'true') return;
        button.dataset.bound = 'true';
        button.addEventListener('click', () => {
            openAccountPoolInvitePicker(
                button.dataset.entryId,
                button.dataset.email,
                button.dataset.seatType || 'default',
            );
        });
    });
    document.querySelectorAll('.account-pool-auto-login-button').forEach(button => {
        if (button.dataset.bound === 'true') return;
        button.dataset.bound = 'true';
        button.addEventListener('click', () => runAccountPoolAutomaticLogin(
            button.dataset.entryId,
            button.dataset.email,
            button.dataset.workspaceId || '',
            button,
        ));
    });
    document.querySelectorAll('.account-pool-json-export-button').forEach(button => {
        if (button.dataset.bound === 'true') return;
        button.dataset.bound = 'true';
        button.addEventListener('click', () => exportAccountPoolJson(
            button.dataset.entryId,
            button.dataset.email,
            button.dataset.workspaceId || '',
            button,
        ));
    });
}

function initAccountPoolExportJobs() {
    document.querySelectorAll('.account-pool-export-status').forEach(label => {
        if (label.dataset.status === 'pending' || label.dataset.status === 'running') {
            const email = label.closest('[data-account-pool-row]')?.querySelector('[data-label="邮箱"]')?.textContent?.trim() || '账号';
            watchAccountPoolExportJob(Number(label.dataset.jobId), email);
        }
    });
}

function initAccountPoolPageSizeControl() {
    const control = document.getElementById('accountPoolPageSize');
    if (!control || control.dataset.bound === 'true') return;
    control.dataset.bound = 'true';
    control.addEventListener('change', event => {
        refreshAccountPoolTable({page: 1, perPage: event.target.value});
    });
}

document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('accountPoolCredentialForm')?.addEventListener(
        'submit',
        submitAccountPoolCredentialForm
    );
    document.getElementById('accountPoolForm')?.addEventListener('submit', submitAccountPoolForm);
    document.getElementById('accountPoolLivenessBtn')?.addEventListener('click', runAccountPoolLiveness);
    initAccountPoolRowActions();
    initAccountPoolExportJobs();
    initAccountPoolPageSizeControl();
    document.getElementById('accountPoolStatusFilter')?.addEventListener('change', event => {
        refreshAccountPoolTable({page: 1, statusFilter: event.target.value});
    });
    document.querySelector('.account-pool-toolbar-actions .search-form')?.addEventListener('submit', event => {
        event.preventDefault();
        refreshAccountPoolTable({page: 1, search: event.currentTarget.elements.search.value});
    });
    initAccountPoolColumnToggler();
    initAccountPoolColumnDropdown();
    formatQuotaResetTimes();
});
