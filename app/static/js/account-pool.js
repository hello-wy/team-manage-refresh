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
        setTimeout(() => location.reload(), 300);
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
        document.querySelector(`[data-account-pool-row="${entryId}"]`)?.remove();
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
        setTimeout(() => location.reload(), 300);
    } catch (error) {
        showToast(error.message || '验活失败', 'error');
    } finally {
        button.disabled = false;
        button.querySelector('span').textContent = '立即验活';
    }
}

function accountPoolTeamLabel(team) {
    return `${team.name || `Team #${team.id}`} · ID ${team.id}`;
}

function openAccountPoolTeamPicker(entryId, email, teams) {
    const options = document.getElementById('accountPoolTeamPickerOptions');
    document.getElementById('accountPoolTeamPickerEmail').textContent = email;
    options.innerHTML = teams.map(team => `
        <button type="button" class="btn btn-secondary account-pool-team-picker-option" data-team-id="${team.id}">
            <span>${escapeHtml(accountPoolTeamLabel(team))}</span>
            <small>${escapeHtml(team.email || 'Team 邮箱未知')} · ${team.status === 'joined' ? '已加入' : '待接受邀请'}</small>
        </button>
    `).join('');
    options.querySelectorAll('[data-team-id]').forEach(button => {
        button.addEventListener('click', () => {
            hideModal('accountPoolTeamPickerModal');
            runAccountPoolAutomaticLogin(entryId, email, Number(button.dataset.teamId), button);
        });
    });
    showModal('accountPoolTeamPickerModal');
    if (window.lucide) lucide.createIcons();
}

async function runAccountPoolAutomaticLogin(entryId, email, teamId, button) {
    if (!confirm(`确定让 ${email} 自动登录并获取 Team #${teamId} 的 JSON 吗？`)) return;
    const original = button.innerHTML;
    button.disabled = true;
    if (button.querySelector('i')) button.querySelector('i').setAttribute('data-lucide', 'loader-circle');
    try {
        const response = await fetch(`/admin/account-pool/${entryId}/automatic-login`, {
            method: 'POST', credentials: 'same-origin',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({team_id: teamId})
        });
        if (!response.ok) {
            const payload = await response.json();
            throw new Error(payload.error || '自动登录导出失败');
        }
        const blob = await response.blob();
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = `sub2api-account-pool-${entryId}.json`;
        link.click();
        URL.revokeObjectURL(url);
        showToast(`${email} 的 JSON 已下载`, 'success');
    } catch (error) {
        showToast(error.message || '自动登录导出失败', 'error');
    } finally {
        button.disabled = false;
        button.innerHTML = original;
        if (window.lucide) lucide.createIcons();
    }
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
        setTimeout(() => location.reload(), 500);
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

    const storageKey = 'account_pool_list_columns';
    const hiddenColumns = JSON.parse(localStorage.getItem(storageKey) || '[]');
    container.innerHTML = '<div class="dropdown-header">显示/隐藏列</div>';

    table.querySelectorAll('thead th').forEach((header, index) => {
        const label = header.innerText.trim();
        if (!label || label === '操作') return;

        const visible = !hiddenColumns.includes(index);
        setAccountPoolColumnVisibility(table, index, visible);

        const item = document.createElement('label');
        item.className = 'dropdown-item';
        item.innerHTML = `<input type="checkbox" ${visible ? 'checked' : ''}><span>${escapeHtml(label)}</span>`;
        item.querySelector('input').addEventListener('change', (event) => {
            const nextVisible = event.target.checked;
            setAccountPoolColumnVisibility(table, index, nextVisible);
            const currentHidden = JSON.parse(localStorage.getItem(storageKey) || '[]');
            const hiddenIndex = currentHidden.indexOf(index);
            if (nextVisible && hiddenIndex >= 0) currentHidden.splice(hiddenIndex, 1);
            if (!nextVisible && hiddenIndex < 0) currentHidden.push(index);
            localStorage.setItem(storageKey, JSON.stringify(currentHidden));
        });
        container.appendChild(item);
    });
}

function setAccountPoolColumnVisibility(table, index, visible) {
    const display = visible ? '' : 'none';
    table.querySelector(`thead th:nth-child(${index + 1})`).style.display = display;
    table.querySelectorAll(`tbody tr td:nth-child(${index + 1})`).forEach((cell) => {
        cell.style.display = display;
    });
}

document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.account-pool-history-btn').forEach(button => {
        button.addEventListener('click', () => {
            loadAccountPoolHistory(button.dataset.entryId, button.dataset.email);
        });
    });
    document.getElementById('accountPoolCredentialForm')?.addEventListener(
        'submit',
        submitAccountPoolCredentialForm
    );
    document.getElementById('accountPoolForm')?.addEventListener('submit', submitAccountPoolForm);
    document.getElementById('accountPoolLivenessBtn')?.addEventListener('click', runAccountPoolLiveness);
    document.querySelectorAll('.account-pool-auto-login-button').forEach(button => {
        button.addEventListener('click', () => {
            const teams = JSON.parse(button.dataset.teamOptions || '[]');
            if (teams.length > 1) {
                openAccountPoolTeamPicker(button.dataset.entryId, button.dataset.email, teams);
                return;
            }
            runAccountPoolAutomaticLogin(
                button.dataset.entryId,
                button.dataset.email,
                Number(button.dataset.teamId),
                button,
            );
        });
    });
    initAccountPoolColumnToggler();
});
