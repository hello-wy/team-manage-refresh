const RENEWAL_MIN_DAYS = 1;
const RENEWAL_MAX_DAYS = 365;
const RENEWAL_ALREADY_HANDLED_STATUSES = new Set([400, 404, 409]);

function removeRenewalCard(requestId) {
    document.querySelector(`.renewal-task-card[data-request-id="${requestId}"]`)?.remove();
    const remaining = document.querySelectorAll('.renewal-task-card').length;
    syncRenewalBadge(remaining);
    if (remaining === 0) renderRenewalRequestsList([]);
}

function setRenewalButton(btn, {busy, html}) {
    if (!btn || !document.body.contains(btn)) return;
    btn.disabled = busy;
    btn.innerHTML = html;
    if (window.lucide) lucide.createIcons();
}

function handleRenewalResponse({response, result, requestId, label}) {
    if (response.ok && result && result.success) {
        showToast(result.message || `${label}成功`, 'success');
        removeRenewalCard(requestId);
        return;
    }
    const httpHint = describeRenewalHttpError(response.status);
    const serverError = result && (result.error || result.detail || result.message);
    showToast(serverError || httpHint || `${label}失败`, 'error');
    if (RENEWAL_ALREADY_HANDLED_STATUSES.has(response.status)) removeRenewalCard(requestId);
}

async function submitRenewalAction({requestId, btn, action, payload, label}) {
    const original = btn ? btn.innerHTML : '';
    try {
        setRenewalButton(btn, {
            busy: true,
            html: '<i data-lucide="loader-2" class="spin" style="width: 14px; height: 14px;"></i> 处理中...',
        });
        const response = await fetch(`/admin/renewal-requests/${requestId}/${action}`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload),
        });
        const result = await response.json();
        handleRenewalResponse({response, result, requestId, label});
    } catch (err) {
        console.error(`${label}失败`, err);
        showToast(`${label}失败：${err.message}`, 'error');
    } finally {
        setRenewalButton(btn, {busy: false, html: original});
    }
}

async function handleRenewalExtend(requestId, btn) {
    const daysInput = document.getElementById(`renewalModalDays-${requestId}`);
    const extension_days = parseInt(daysInput?.value || '0', 10);
    if (!extension_days || extension_days < RENEWAL_MIN_DAYS || extension_days > RENEWAL_MAX_DAYS) {
        showToast(`请输入 ${RENEWAL_MIN_DAYS}~${RENEWAL_MAX_DAYS} 的续期天数`, 'error');
        return;
    }
    await submitRenewalAction({requestId, btn, action: 'extend', payload: {extension_days}, label: '续期'});
}

async function handleRenewalIgnore(requestId, btn) {
    if (!await confirmPageAction('确定忽略这条续期请求吗？')) return;
    await submitRenewalAction({requestId, btn, action: 'ignore', payload: {}, label: '忽略'});
}
