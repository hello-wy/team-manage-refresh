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

async function deleteAccountPoolEntry(entryId, email) {
    if (!confirm(`确定从账号号池删除 ${email} 吗？加入历史也会一并删除。`)) return;
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

document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.account-pool-history-btn').forEach(button => {
        button.addEventListener('click', () => {
            loadAccountPoolHistory(button.dataset.entryId, button.dataset.email);
        });
    });
    document.getElementById('accountPoolForm')?.addEventListener('submit', submitAccountPoolForm);
});
