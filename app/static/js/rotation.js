const rotationColumnKey = 'rotation_columns_v1';
const rotationHeaders = [...document.querySelectorAll('.rotation-table thead th')]
    .map((header) => header.textContent.trim());

async function rotationRequest(url, options = {}) {
    const response = await fetch(url, options);
    if (!response.headers.get('content-type')?.includes('application/json')) {
        throw new Error(`HTTP ${response.status}: 服务端响应格式错误`);
    }
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || '操作失败');
    return result;
}

function rotationMessage(section, message) {
    section.querySelector('.rotation-result').textContent = message;
}

function hiddenRotationColumns() {
    const saved = localStorage.getItem(rotationColumnKey);
    if (!saved) return [];
    const columns = JSON.parse(saved);
    if (!Array.isArray(columns) || !columns.every(Number.isInteger)) {
        throw new TypeError('轮转列设置格式错误');
    }
    return columns;
}

function applyRotationColumns(section) {
    const hidden = hiddenRotationColumns();
    section.querySelectorAll('.rotation-table tr').forEach((row) => {
        [...row.children].forEach((cell, index) => {
            cell.style.display = hidden.includes(index) ? 'none' : '';
        });
    });
}

function initRotationColumns() {
    const menu = document.getElementById('rotation-columns-menu');
    const button = document.getElementById('rotation-columns-button');
    menu.innerHTML = '<div class="dropdown-header">显示/隐藏列</div>';
    rotationHeaders.forEach((label, index) => {
        const item = document.createElement('label');
        item.className = 'dropdown-item';
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.checked = !hiddenRotationColumns().includes(index);
        checkbox.addEventListener('change', () => {
            const hidden = new Set(hiddenRotationColumns());
            if (checkbox.checked) hidden.delete(index);
            else hidden.add(index);
            localStorage.setItem(rotationColumnKey, JSON.stringify([...hidden]));
            document.querySelectorAll('.rotation-team').forEach(applyRotationColumns);
        });
        const text = document.createElement('span');
        text.textContent = label;
        item.append(checkbox, text);
        menu.append(item);
    });
    button.addEventListener('click', () => {
        const open = menu.classList.toggle('show');
        button.setAttribute('aria-expanded', String(open));
    });
}

async function loadRotationActions(section) {
    const teamId = section.dataset.teamId;
    const target = section.querySelector('.rotation-actions');
    try {
        const result = await rotationRequest(`/admin/teams/${teamId}/rotation/actions`);
        target.textContent = result.actions.length
            ? result.actions.map((row) => `${row.created_at}  ${row.email}  ${row.action}  ${row.status}  ${row.result || ''}`).join('\n')
            : '暂无动作记录';
        target.style.whiteSpace = 'pre-line';
    } catch (error) {
        target.textContent = error.message;
    }
}

function updateRotationTable(section, html) {
    const fragment = document.createElement('template');
    fragment.innerHTML = html.trim();
    const updated = fragment.content.firstElementChild;
    section.querySelector('.rotation-table').replaceWith(updated.querySelector('.rotation-table'));
    const currentBalance = section.querySelector('.table-section-copy .text-muted');
    const updatedBalance = updated.querySelector('.table-section-copy .text-muted');
    if (currentBalance && updatedBalance) currentBalance.replaceWith(updatedBalance);
    else if (currentBalance) currentBalance.remove();
    else if (updatedBalance) section.querySelector('.table-section-copy').append(updatedBalance);
    applyRotationColumns(section);
    formatQuotaResetTimes();
    lucide.createIcons();
    void loadRotationActions(section);
}

async function refreshRotation(section) {
    const button = section.querySelector('.rotation-refresh');
    button.disabled = true;
    rotationMessage(section, '正在同步 Team 成员与额度...');
    try {
        const result = await rotationRequest(`/admin/teams/${section.dataset.teamId}/rotation/refresh`, {method: 'POST'});
        updateRotationTable(section, result.html);
        rotationMessage(section, '已同步最新 Team 状态与额度');
    } catch (error) {
        rotationMessage(section, error.message);
        void loadRotationActions(section);
    } finally {
        button.disabled = false;
    }
}

async function changeRotationMode(section, input) {
    const enabled = input.checked;
    input.disabled = true;
    try {
        await rotationRequest(`/admin/teams/${section.dataset.teamId}/rotation/config`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({mode: enabled ? 'auto' : 'off'}),
        });
        section.querySelector('.rotation-mode-label').textContent = enabled ? '已开启' : '已关闭';
        section.querySelector('.rotation-run').disabled = !enabled;
        rotationMessage(section, enabled ? '已开启轮转' : '已关闭轮转');
    } catch (error) {
        input.checked = !enabled;
        rotationMessage(section, error.message);
    } finally {
        input.disabled = false;
    }
}

async function refreshRotationQuota(section, row, button) {
    button.disabled = true;
    const teamId = section.dataset.teamId;
    const email = encodeURIComponent(row.dataset.email);
    try {
        const result = await rotationRequest(`/admin/teams/${teamId}/rotation/members/${email}/quota`, {method: 'POST'});
        row.querySelector('.rotation-quota-content').innerHTML = result.html;
        row.querySelector('.rotation-observed-at').textContent = result.observed_at;
        row.querySelector('.rotation-blocked-reason').textContent = result.blocked_reason;
        formatQuotaResetTimes();
        rotationMessage(section, `${row.dataset.email} 额度已更新`);
    } catch (error) {
        rotationMessage(section, `${row.dataset.email}: ${error.message}`);
    } finally {
        button.disabled = false;
    }
}

async function previewRotation(section) {
    try {
        const result = await rotationRequest(`/admin/teams/${section.dataset.teamId}/rotation/preview`, {method: 'POST'});
        if (!result.seat_balance?.success) throw new Error(result.seat_balance?.error || '席位余额未知');
        const action = result.next_action;
        rotationMessage(section, action ? `${action.email}: ${action.reason}` : '当前没有可执行动作');
    } catch (error) {
        rotationMessage(section, error.message);
    }
}

async function runRotation(section) {
    const button = section.querySelector('.rotation-run');
    button.disabled = true;
    try {
        const result = await rotationRequest(`/admin/teams/${section.dataset.teamId}/rotation/run`, {method: 'POST'});
        rotationMessage(section, `执行状态: ${result.status}`);
        await refreshRotation(section);
    } catch (error) {
        rotationMessage(section, error.message);
    } finally {
        button.disabled = false;
    }
}

document.querySelector('.rotation-page')?.addEventListener('click', (event) => {
    const section = event.target.closest('.rotation-team');
    if (!section) return;
    const button = event.target.closest('button');
    if (button?.matches('.rotation-refresh')) void refreshRotation(section);
    if (button?.matches('.rotation-preview')) void previewRotation(section);
    if (button?.matches('.rotation-run')) void runRotation(section);
    if (button?.matches('.rotation-quota-refresh')) {
        void refreshRotationQuota(section, button.closest('tr'), button);
    }
});

document.querySelector('.rotation-page')?.addEventListener('change', (event) => {
    if (event.target.matches('.rotation-mode')) {
        void changeRotationMode(event.target.closest('.rotation-team'), event.target);
    }
});

initRotationColumns();
document.querySelectorAll('.rotation-team').forEach(applyRotationColumns);
formatQuotaResetTimes();
async function syncRotationOnEntry() {
    for (const section of document.querySelectorAll('.rotation-team')) {
        await refreshRotation(section);
    }
}
void syncRotationOnEntry();
