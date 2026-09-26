const rotationColumnStorageKey = 'rotation_list_columns_v2';

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

function loadRotationHiddenColumns() {
    const saved = localStorage.getItem(rotationColumnStorageKey);
    if (!saved) return new Set();

    try {
        const columns = JSON.parse(saved);
        if (!Array.isArray(columns)) throw new TypeError('列偏好不是数组');
        return new Set(columns.filter(Number.isInteger));
    } catch (error) {
        console.error('额度轮转列设置偏好无效，已重置。', error);
        localStorage.removeItem(rotationColumnStorageKey);
        showToast('列设置偏好无效，已重置', 'warning');
        return new Set();
    }
}

function applyRotationColumns(section) {
    const hidden = loadRotationHiddenColumns();
    section.querySelectorAll('.rotation-table tr').forEach((row) => {
        [...row.children].forEach((cell, index) => {
            cell.hidden = hidden.has(index);
        });
    });
}

function initRotationColumnToggler() {
    const table = document.querySelector('.rotation-table');
    const menu = document.getElementById('rotation-columns-menu');
    if (!table || !menu) return;

    const hiddenColumns = loadRotationHiddenColumns();
    menu.innerHTML = '<div class="dropdown-header">显示/隐藏列</div>';
    table.querySelectorAll('thead th').forEach((header, index) => {
        const label = header.innerText.trim();
        if (!label) return;

        const visible = !hiddenColumns.has(index);
        document.querySelectorAll('.rotation-table').forEach((rotationTable) => {
            setRotationColumnVisibility(rotationTable, index, visible);
        });
        const item = document.createElement('label');
        item.className = 'dropdown-item';
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.checked = visible;
        checkbox.addEventListener('change', () => {
            const hidden = loadRotationHiddenColumns();
            if (checkbox.checked) hidden.delete(index);
            else hidden.add(index);
            localStorage.setItem(rotationColumnStorageKey, JSON.stringify([...hidden].sort((a, b) => a - b)));
            document.querySelectorAll('.rotation-team').forEach(applyRotationColumns);
        });
        const text = document.createElement('span');
        text.textContent = label;
        item.append(checkbox, text);
        menu.append(item);
    });
}

function setRotationColumnVisibility(table, index, visible) {
    table.querySelectorAll(
        `thead th:nth-child(${index + 1}), tbody tr td:nth-child(${index + 1})`
    ).forEach((cell) => {
        cell.hidden = !visible;
    });
}

function initRotationColumnDropdown() {
    const menu = document.getElementById('rotation-columns-menu');
    const button = document.getElementById('rotation-columns-button');
    if (!menu || !button) return;

    button.addEventListener('click', () => {
        const shouldShow = !menu.classList.contains('show');
        closeFloatingDropdowns();
        button.setAttribute('aria-expanded', 'false');
        if (!shouldShow) return;
        menu.classList.add('show');
        button.setAttribute('aria-expanded', 'true');
        positionFloatingDropdown(menu, button);
        requestAnimationFrame(() => positionFloatingDropdown(menu, button));
    });
    document.addEventListener('click', (event) => {
        if (!menu.classList.contains('show')) return;
        if (button.contains(event.target) || menu.contains(event.target)) return;
        menu.classList.remove('show');
        button.setAttribute('aria-expanded', 'false');
    });
    const reposition = () => {
        if (menu.classList.contains('show')) positionFloatingDropdown(menu, button);
    };
    window.addEventListener('resize', reposition);
    window.addEventListener('scroll', reposition, true);
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

initRotationColumnToggler();
initRotationColumnDropdown();
document.querySelectorAll('.rotation-team').forEach(applyRotationColumns);
formatQuotaResetTimes();
async function syncRotationOnEntry() {
    for (const section of document.querySelectorAll('.rotation-team')) {
        await refreshRotation(section);
    }
}
void syncRotationOnEntry();
