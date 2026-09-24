async function scanAccountPoolWorkspace(button) {
    const original = button.innerHTML;
    button.disabled = true;
    button.innerHTML = '<i data-lucide="loader-circle"></i>';
    if (window.lucide) lucide.createIcons();
    try {
        const response = await fetch(
            `/admin/account-pool/${button.dataset.entryId}/workspace-scan`,
            {method: 'POST', credentials: 'same-origin'},
        );
        const payload = await response.json();
        if (!response.ok || !payload.success) {
            throw new Error(payload.error || '获取当前 Team 失败');
        }
        const count = payload.workspace?.available_workspaces?.length || 0;
        showToast(`${button.dataset.email} 已刷新 ${count} 个空间的状态和 JSON`, 'success');
        await refreshAccountPoolTable();
    } catch (error) {
        showToast(error.message || '获取当前 Team 失败', 'error');
    } finally {
        button.disabled = false;
        button.innerHTML = original;
        if (window.lucide) lucide.createIcons();
    }
}

async function selectAccountPoolWorkspace(control) {
    control.disabled = true;
    try {
        const response = await fetch(
            `/admin/account-pool/${control.dataset.entryId}/workspace-selection`,
            {method: 'PATCH', credentials: 'same-origin',
             headers: {'Content-Type': 'application/json'},
             body: JSON.stringify({workspace_id: control.value})},
        );
        const result = await response.json();
        if (!response.ok || !result.success) throw new Error(result.detail || result.error || '选择 Team 失败');
        await refreshAccountPoolTable();
    } catch (error) {
        showToast(error.message || '选择 Team 失败', 'error');
        await refreshAccountPoolTable();
    }
}

function initAccountPoolWorkspaceButtons() {
    document.querySelectorAll('.account-pool-workspace-selector').forEach(control => {
        if (control.dataset.bound === 'true') return;
        control.dataset.bound = 'true';
        control.addEventListener('change', () => selectAccountPoolWorkspace(control));
    });
    document.querySelectorAll('.account-pool-workspace-scan-button').forEach(button => {
        if (button.dataset.bound === 'true') return;
        button.dataset.bound = 'true';
        button.addEventListener('click', () => scanAccountPoolWorkspace(button));
    });
}

window.initAccountPoolWorkspaceButtons = initAccountPoolWorkspaceButtons;

document.addEventListener('DOMContentLoaded', initAccountPoolWorkspaceButtons);
