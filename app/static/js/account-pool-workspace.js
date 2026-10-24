async function selectAccountPoolWorkspace(control) {
    const previousId = control.dataset.selectedId;
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
        control.dataset.selectedId = result.workspace_id;
        closeAccountPoolDetails();
        await refreshAccountPoolTable();
    } catch (error) {
        control.value = previousId;
        showToast(error.message || '选择 Team 失败', 'error');
    } finally {
        control.disabled = false;
    }
}

function initAccountPoolWorkspaceButtons() {
    document.querySelectorAll('.account-pool-workspace-selector').forEach(control => {
        if (control.dataset.bound === 'true') return;
        control.dataset.bound = 'true';
        control.addEventListener('change', () => selectAccountPoolWorkspace(control));
    });
}

window.initAccountPoolWorkspaceButtons = initAccountPoolWorkspaceButtons;

document.addEventListener('DOMContentLoaded', initAccountPoolWorkspaceButtons);
