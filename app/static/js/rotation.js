async function rotationRequest(teamId, action) {
    const response = await fetch(`/admin/teams/${teamId}/rotation/${action}`, { method: 'POST' });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || '操作失败');
    return result;
}

function showRotationResult(teamId, message) {
    const section = document.querySelector(`[data-team-id="${teamId}"].content-section`);
    section.querySelector('.rotation-result').textContent = message;
}

async function refreshRotation(teamId) {
    try {
        const result = await rotationRequest(teamId, 'refresh');
        showRotationResult(teamId, `已同步 ${result.refreshed} 位成员，正在更新页面`);
        location.reload();
    } catch (error) { showRotationResult(teamId, error.message); }
}

async function previewRotation(teamId) {
    try {
        const result = await rotationRequest(teamId, 'preview');
        const action = result.next_action;
        showRotationResult(teamId, action ? `${action.email}: ${action.reason}` : '当前没有可执行动作');
    } catch (error) { showRotationResult(teamId, error.message); }
}

async function runRotation(teamId) {
    try {
        const result = await rotationRequest(teamId, 'run');
        showRotationResult(teamId, `执行状态: ${result.status}`);
        location.reload();
    } catch (error) { showRotationResult(teamId, error.message); }
}

document.querySelectorAll('.rotation-actions').forEach(async (element) => {
    const response = await fetch(`/admin/teams/${element.dataset.teamId}/rotation/actions`);
    if (!response.ok) return;
    const result = await response.json();
    element.textContent = result.actions.length
        ? result.actions.map((row) => `${row.created_at}  ${row.email}  ${row.action}  ${row.status}  ${row.result || ''}`).join('\n')
        : '暂无动作记录';
    element.style.whiteSpace = 'pre-line';
});
