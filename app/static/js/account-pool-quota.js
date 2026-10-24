function accountPoolQuotaFeedback(button, text) {
    button.closest('td').querySelector('.account-pool-quota-feedback').textContent = text;
}

function applyAccountPoolQuota(button, result) {
    if (!button.isConnected) return;
    if (!result) {
        accountPoolQuotaFeedback(button, '未返回该账号的额度，请重试');
        return;
    }
    if (result.workspace_id !== button.dataset.workspaceId) {
        accountPoolQuotaFeedback(button, '账号的 Team 已变化，请刷新列表');
        return;
    }
    button.closest('td').querySelector('.account-pool-quota-content').innerHTML = result.html;
    accountPoolQuotaFeedback(button, result.status === 'ok' ? '刚刚更新' : result.error || '暂未获取额度');
    formatQuotaResetTimes();
}

async function refreshAccountPoolQuota(button) {
    if (button.disabled) return;
    button.disabled = true;
    accountPoolQuotaFeedback(button, '正在读取…');
    try {
        const response = await fetch(`/admin/account-pool/${button.dataset.entryId}/usage`, {
            method: 'POST', credentials: 'same-origin', cache: 'no-store',
        });
        const result = await response.json();
        if (!response.ok || !result.success) throw new Error(result.detail || result.error || '刷新额度失败');
        applyAccountPoolQuota(button, result);
    } catch (error) {
        accountPoolQuotaFeedback(button, error.message || '刷新额度失败，请重试');
    } finally {
        button.disabled = false;
    }
}

async function refreshVisibleAccountPoolQuotas(buttons) {
    if (!buttons.length) return;
    for (const button of buttons) {
        button.disabled = true;
        accountPoolQuotaFeedback(button, '正在读取…');
    }
    try {
        const response = await fetch('/admin/account-pool/usage', {
            method: 'POST', credentials: 'same-origin', cache: 'no-store',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({entry_ids: buttons.map(button => Number(button.dataset.entryId))}),
        });
        const result = await response.json();
        if (!response.ok || !result.success) throw new Error(result.detail || result.error || '读取额度失败');
        const byId = new Map(result.entries.map(entry => [String(entry.entry_id), entry]));
        for (const button of buttons) applyAccountPoolQuota(button, byId.get(button.dataset.entryId));
    } catch (error) {
        for (const button of buttons) accountPoolQuotaFeedback(button, error.message || '读取额度失败，请重试');
    } finally {
        for (const button of buttons) button.disabled = false;
    }
}

function initAccountPoolQuota() {
    const pending = [];
    for (const button of document.querySelectorAll('.account-pool-quota-refresh')) {
        if (button.dataset.bound === 'true') continue;
        button.dataset.bound = 'true';
        button.addEventListener('click', () => refreshAccountPoolQuota(button));
        if (!button.disabled) pending.push(button);
    }
    return refreshVisibleAccountPoolQuotas(pending);
}

window.initAccountPoolQuota = initAccountPoolQuota;
