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
        const workspaceId = payload.workspace?.workspace_id;
        const isPersonal = payload.workspace?.status === 'personal_account';
        const message = isPersonal
            ? `${button.dataset.email} 当前为个人账户`
            : workspaceId
                ? `${button.dataset.email} 当前 Workspace：${workspaceId}`
                : `${button.dataset.email} 当前未加入 Workspace`;
        showToast(message, 'success');
        setTimeout(() => location.reload(), 300);
    } catch (error) {
        showToast(error.message || '获取当前 Team 失败', 'error');
    } finally {
        button.disabled = false;
        button.innerHTML = original;
        if (window.lucide) lucide.createIcons();
    }
}

document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.account-pool-workspace-scan-button').forEach(button => {
        button.addEventListener('click', () => scanAccountPoolWorkspace(button));
    });
});
