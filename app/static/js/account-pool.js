function accountPoolFormatDate(value) {
    if (!value) return '-';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return date.toLocaleString('zh-CN', {
        year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit'
    });
}

async function copyAccountPoolCredential(email) {
    try {
        const credentials = await window.accountPoolCredentialStore.load(email, {fresh: true});
        if (!credentials.password) throw new Error('该账号未保存登录密码，请先补充账号凭据');
        if (!credentials.two_factor_secret) throw new Error('该账号未保存 2FA 密钥，请先补充账号凭据');
        await copyToClipboard([credentials.email || email, credentials.password, credentials.two_factor_secret].join('----'));
    } catch (error) {
        showToast(error.message || '复制账号凭据失败', 'error');
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
    if (!await confirmAccountPoolAction(`确定永久删除 ${email} 吗？账号信息和加入历史都会从数据库删除，且无法恢复。`)) return;
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

async function checkAccountPoolEntryLiveness(button) {
    if (button.disabled) return;
    button.disabled = true;
    showToast(`正在验活 ${button.dataset.email}…`, 'info');
    try {
        const response = await fetch(`/admin/account-pool/${button.dataset.entryId}/liveness`, {
            method: 'POST', credentials: 'same-origin', cache: 'no-store',
        });
        const result = await response.json();
        if (!response.ok || !result.success) throw new Error(result.error || result.detail || '验活失败');
        showToast(result.message, result.status === 'alive' ? 'success' : 'warning');
        await refreshAccountPoolTable();
    } catch (error) {
        showToast(error.message || '验活失败，请重试', 'error');
    } finally {
        button.disabled = false;
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
    if (!await confirmAccountPoolAction(`确定邀请 ${email} 加入所选 Team 吗？`)) return;
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
    if (!await confirmAccountPoolAction(`确定重新登录 ${email} 并导入配置的 sub2api 吗？${targetHint}`)) return;
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
    if (!await confirmAccountPoolAction(`确定重新登录 ${email} 并导出最新 JSON 吗？`)) return;
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

let accountPoolHistoryRequest = null;
let accountPoolDetailsTrigger = null;

function closeAccountPoolDetails() {
    accountPoolHistoryRequest?.abort();
    hideModal('accountPoolHistoryModal');
    document.getElementById('accountPoolDetailsContent').replaceChildren();
    accountPoolDetailsTrigger?.focus();
}

async function loadAccountPoolHistory(entryId, email) {
    accountPoolHistoryRequest?.abort();
    const controller = new AbortController();
    accountPoolHistoryRequest = controller;
    const content = document.getElementById('accountPoolHistoryContent');
    const details = document.getElementById('accountPoolDetailsContent');
    const template = document.getElementById(`accountPoolDetails-${entryId}`);
    details.replaceChildren(...(template ? [template.content.cloneNode(true)] : []));
    document.getElementById('accountPoolHistoryEmail').textContent = email;
    content.textContent = '加载中...';
    initAccountPoolRowActions();
    window.initAccountPoolRotateButtons?.();
    window.initAccountPoolWorkspaceButtons?.();
    // Close this dialog before a detail action opens its own dialog.
    details.querySelector('.account-pool-detail-actions')?.addEventListener('click', event => {
        if (event.target.closest('button')) hideModal('accountPoolHistoryModal');
    }, {capture: true});
    showModal('accountPoolHistoryModal');
    if (window.lucide) lucide.createIcons();
    document.querySelector('#accountPoolHistoryModal .modal-close')?.focus();
    try {
        const response = await fetch(`/admin/account-pool/${entryId}/history`, {signal: controller.signal});
        const payload = await response.json();
        if (controller.signal.aborted) return;
        if (!response.ok || !payload.success) throw new Error(payload.error || '加载历史失败');
        renderAccountPoolHistory(content, payload.data.histories || []);
    } catch (error) {
        if (error.name === 'AbortError') return;
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
    const rotate2fa = document.getElementById('accountPoolAutoRotate').checked;
    if (!content) return showToast('请先输入邮箱', 'warning');
    button.disabled = true;
    try {
        const response = await fetch('/admin/account-pool', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({content, rotate_2fa: rotate2fa})
        });
        const payload = await response.json();
        if (!response.ok || !payload.success) throw new Error(payload.message || payload.error || '添加失败');
        showToast(payload.message, 'success');
        input.value = '';
        [...(payload.added || []), ...(payload.restored || []), ...(payload.updated || [])]
            .forEach(email => window.accountPoolCredentialStore.clear(email));
        if (payload.rotation_error) {
            showToast(payload.rotation_error, 'warning');
            startAccountPoolRotationWatch(null, [payload.rotation_error]);
        } else if (payload.rotation) {
            const rotation = payload.rotation;
            const notes = rotation.skipped.map(item => `${item.email}：跳过自动更换 · ${item.reason}`);
            if (!rotation.batch_id && !notes.length) notes.push('本次没有新增或恢复的账号，未自动更换 2FA。');
            startAccountPoolRotationWatch(rotation.batch_id, notes);
        }
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
    if (!container) return;
    document.getElementById('accountPoolColumnToggleBtn').disabled = !table;
    if (!table) {
        container.replaceChildren();
        closeAccountPoolColumnDropdown();
        return;
    }

    const storageKey = 'account_pool_list_columns_v4';
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
let accountPoolRequestedUrl = null;

function getAccountPoolTableUrl({page, perPage, statusFilter, search, teamFilter, seatFilter, sortBy} = {}) {
    const url = new URL('/admin/account-pool', window.location.origin);
    const current = new URLSearchParams(accountPoolRequestedUrl?.search ?? window.location.search);
    const searchInput = document.querySelector('#accountPoolSearchForm input[name="search"]');
    const size = document.getElementById('accountPoolPageSize');
    const sort = document.getElementById('accountPoolSort');
    for (const [key, value] of Object.entries({
        page: page ?? current.get('page') ?? 1,
        per_page: perPage ?? current.get('per_page') ?? size?.value ?? 20,
        search: search ?? current.get('search') ?? searchInput?.value?.trim() ?? '',
        status_filter: statusFilter ?? current.get('status_filter') ?? '',
        team_filter: teamFilter ?? current.get('team_filter') ?? '',
        seat_filter: seatFilter ?? current.get('seat_filter') ?? '',
        sort_by: sortBy ?? current.get('sort_by') ?? sort?.value ?? 'team',
    })) url.searchParams.set(key, String(value));
    return url;
}

function syncAccountPoolFilters(fragment, url) {
    const filters = document.getElementById('accountPoolFilters');
    const nextFilters = fragment.getElementById('accountPoolFilters');
    if (filters && nextFilters) filters.replaceChildren(...nextFilters.childNodes);
    const form = document.getElementById('accountPoolSearchForm');
    for (const key of ['search', 'team_filter', 'seat_filter', 'status_filter', 'sort_by', 'per_page']) {
        if (form?.elements[key]) form.elements[key].value = url.searchParams.get(key) || '';
    }
    const sort = document.getElementById('accountPoolSort');
    if (sort) sort.value = url.searchParams.get('sort_by') || 'team';
}

function initAccountPoolFilters() {
    document.getElementById('accountPoolFilters')?.addEventListener('click', event => {
        const link = event.target.closest('a[href]');
        if (!link || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        const params = new URL(link.href).searchParams;
        const kind = link.dataset.filterKind;
        const filters = kind === 'reset' ? {teamFilter: '', seatFilter: '', statusFilter: '', search: ''}
            : kind === 'team' ? {teamFilter: params.get('team_filter') || '', statusFilter: ''}
            : {seatFilter: params.get('seat_filter') || ''};
        refreshAccountPoolTable({page: 1, ...filters});
    });
    document.getElementById('accountPoolSort')?.addEventListener('change', event => {
        refreshAccountPoolTable({page: 1, sortBy: event.target.value});
    });
    document.getElementById('accountPoolSearchForm')?.addEventListener('submit', event => {
        event.preventDefault();
        refreshAccountPoolTable({page: 1, search: event.currentTarget.elements.search.value});
    });
    const toggle = document.getElementById('accountPoolImportToggle');
    const panel = document.getElementById('accountPoolImportPanel');
    const setImportOpen = open => {
        panel.hidden = !open;
        toggle.setAttribute('aria-expanded', String(open));
        if (open) document.getElementById('accountPoolEmails').focus();
        else toggle.focus();
    };
    toggle?.addEventListener('click', () => setImportOpen(panel.hidden));
    document.getElementById('accountPoolImportClose')?.addEventListener('click', () => setImportOpen(false));
}

async function refreshAccountPoolTable(options = {}) {
    const region = document.getElementById('accountPoolTableRegion');
    if (!region) return;
    accountPoolTableRequest?.abort();
    const controller = new AbortController();
    accountPoolTableRequest = controller;
    const url = getAccountPoolTableUrl(options);
    accountPoolRequestedUrl = url;
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
        const nextPage = documentFragment.getElementById('accountPoolSearchForm');
        for (const key of ['team_filter', 'seat_filter', 'status_filter', 'sort_by', 'per_page']) {
            if (nextPage?.elements[key]) url.searchParams.set(key, nextPage.elements[key].value);
        }
        url.searchParams.set('page', nextRegion.dataset.currentPage);
        const previous = new URLSearchParams(window.location.search);
        if (['team_filter', 'seat_filter', 'status_filter', 'search'].some(key =>
            (previous.get(key) || '') !== (url.searchParams.get(key) || ''))) {
            window.clearAccountPoolSelection?.();
        }
        region.replaceWith(nextRegion);
        syncAccountPoolFilters(documentFragment, url);
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
        if (options.scrollToTable) nextRegion.scrollIntoView({block: 'start'});
    } catch (error) {
        if (error.name !== 'AbortError') showToast(error.message || '账号列表加载失败', 'error');
    } finally {
        if (accountPoolTableRequest === controller) {
            accountPoolRequestedUrl = null;
            document.getElementById('accountPoolTableRegion')?.classList.remove('is-loading');
        }
    }
}
window.refreshAccountPoolTable = refreshAccountPoolTable;

function initAccountPoolActionTooltips() {
    const tooltip = document.createElement('div');
    tooltip.id = 'accountPoolActionTooltip';
    tooltip.className = 'account-pool-action-tooltip';
    tooltip.setAttribute('role', 'tooltip');
    tooltip.hidden = true;
    document.body.appendChild(tooltip);
    let active = null;
    let described = null;

    const hide = () => {
        tooltip.hidden = true;
        described?.removeAttribute('aria-describedby');
        active = null;
        described = null;
    };
    const show = event => {
        const trigger = event.target.closest?.('[data-pool-tooltip]');
        if (!trigger || trigger === active) return;
        hide();
        active = trigger;
        described = trigger.hasAttribute('tabindex') ? trigger : trigger.querySelector('button');
        described?.setAttribute('aria-describedby', tooltip.id);
        tooltip.textContent = trigger.dataset.poolTooltip;
        tooltip.hidden = false;
        const anchor = trigger.getBoundingClientRect();
        const box = tooltip.getBoundingClientRect();
        const left = Math.max(12, Math.min(anchor.left + (anchor.width - box.width) / 2, window.innerWidth - box.width - 12));
        const top = anchor.top >= box.height + 20 ? anchor.top - box.height - 8 : anchor.bottom + 8;
        tooltip.style.left = `${left}px`;
        tooltip.style.top = `${top}px`;
    };
    const leave = event => {
        if (active && !active.contains(event.relatedTarget)) hide();
    };
    // Wrappers also receive pointer events when the enclosed button is disabled.
    document.addEventListener('pointerover', show);
    document.addEventListener('pointerout', leave);
    document.addEventListener('focusin', show);
    document.addEventListener('focusout', leave);
    document.addEventListener('click', hide);
    document.addEventListener('keydown', event => { if (event.key === 'Escape') hide(); });
    window.addEventListener('scroll', hide, true);
    window.addEventListener('resize', hide);
}

function initAccountPoolRowActions() {
    document.querySelectorAll('.account-pool-entry-liveness').forEach(button => {
        if (button.dataset.bound === 'true') return;
        button.dataset.bound = 'true';
        button.addEventListener('click', () => checkAccountPoolEntryLiveness(button));
    });
    updateAccountPoolCountdowns();
    document.querySelectorAll('.account-pool-quota-refresh').forEach(button => {
        if (button.dataset.bound === 'true') return;
        button.dataset.bound = 'true';
        button.addEventListener('click', () => refreshAccountPoolQuota(button));
    });
    document.querySelectorAll('.account-pool-history-btn').forEach(button => {
        if (button.dataset.bound === 'true') return;
        button.dataset.bound = 'true';
        button.addEventListener('click', () => {
            accountPoolDetailsTrigger = button;
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

function accountPoolCountdownLabel(value, now = Date.now(), kind = 'replacement') {
    const isExit = kind === 'exit';
    const deadline = Date.parse(value);
    if (!Number.isFinite(deadline)) return isExit ? '下线时间待确认' : '上线时间待确认';
    if (deadline <= now) return isExit ? '等待下线执行' : '等待补位执行';
    const seconds = Math.ceil((deadline - now) / 1000);
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor(seconds % 3600 / 60);
    return `${isExit ? '下线倒计时' : '预计上线倒计时'} ${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
}

function updateAccountPoolCountdowns() {
    document.querySelectorAll('[data-replacement-at]').forEach(node => {
        node.textContent = accountPoolCountdownLabel(node.dataset.replacementAt);
    });
    document.querySelectorAll('[data-exit-at]').forEach(node => {
        node.textContent = accountPoolCountdownLabel(node.dataset.exitAt, Date.now(), 'exit');
    });
}

async function refreshAccountPoolQuota(button) {
    if (button.disabled) return;
    const cell = button.closest('td');
    const feedback = cell.querySelector('.account-pool-quota-feedback');
    button.disabled = true;
    feedback.textContent = '正在读取…';
    try {
        const response = await fetch(`/admin/account-pool/${button.dataset.entryId}/usage`, {
            method: 'POST', credentials: 'same-origin', cache: 'no-store',
        });
        const result = await response.json();
        if (!response.ok || !result.success) throw new Error(result.detail || result.error || '刷新额度失败');
        cell.querySelector('.account-pool-quota-content').innerHTML = result.html;
        if (!cell.querySelector('.quota-progress')) button.remove();
        formatQuotaResetTimes();
        feedback.textContent = result.status === 'ok' ? '刚刚更新' : (result.error || '暂未获取额度');
    } catch (error) {
        feedback.textContent = error.message || '刷新额度失败，请重试';
    } finally {
        button.disabled = false;
    }
}

function initAccountPoolExportJobs() {
    document.querySelectorAll('.account-pool-export-status').forEach(label => {
        if (label.dataset.status === 'pending' || label.dataset.status === 'running') {
            const email = label.closest('[data-account-pool-row]')?.dataset.email || '账号';
            watchAccountPoolExportJob(Number(label.dataset.jobId), email);
        }
    });
}

function initAccountPoolPageSizeControl() {
    const region = document.getElementById('accountPoolTableRegion');
    if (region && region.dataset.paginationBound !== 'true') {
        region.dataset.paginationBound = 'true';
        region.addEventListener('click', event => {
            const link = event.target.closest('.pagination a[href]');
            if (!link || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
            event.preventDefault();
            refreshAccountPoolTable({page: Number(new URL(link.href).searchParams.get('page')) || 1, scrollToTable: true});
        });
    }
    const control = document.getElementById('accountPoolPageSize');
    if (!control || control.dataset.bound === 'true') return;
    control.dataset.bound = 'true';
    control.addEventListener('change', event => {
        refreshAccountPoolTable({page: 1, perPage: event.target.value, scrollToTable: true});
    });
}

document.addEventListener('DOMContentLoaded', () => {
    setInterval(updateAccountPoolCountdowns, 1000);
    initAccountPoolActionTooltips();
    const detailsModal = document.getElementById('accountPoolHistoryModal');
    detailsModal?.addEventListener('click', event => {
        if (event.target === detailsModal) closeAccountPoolDetails();
    });
    detailsModal?.addEventListener('keydown', event => {
        if (event.key === 'Escape') {
            event.preventDefault();
            closeAccountPoolDetails();
        } else if (event.key === 'Tab') {
            const controls = [...detailsModal.querySelectorAll('button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled)')]
                .filter(control => control.getClientRects().length);
            const first = controls[0];
            const last = controls[controls.length - 1];
            if (event.shiftKey && document.activeElement === first) {
                event.preventDefault();
                last?.focus();
            } else if (!event.shiftKey && document.activeElement === last) {
                event.preventDefault();
                first?.focus();
            }
        }
    });
    document.getElementById('accountPoolCredentialForm')?.addEventListener(
        'submit',
        submitAccountPoolCredentialForm
    );
    document.getElementById('accountPoolForm')?.addEventListener('submit', submitAccountPoolForm);
    document.getElementById('accountPoolLivenessBtn')?.addEventListener('click', runAccountPoolLiveness);
    initAccountPoolRowActions();
    initAccountPoolExportJobs();
    initAccountPoolPageSizeControl();
    initAccountPoolFilters();
    initAccountPoolColumnToggler();
    initAccountPoolColumnDropdown();
    formatQuotaResetTimes();
});
