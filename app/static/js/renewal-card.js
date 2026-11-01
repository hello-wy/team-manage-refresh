function renderRenewalDetails(item, remaining) {
    return [
        ['用户邮箱', `<strong>${escapeHtml(item.email)}</strong>`],
        ['兑换码', `<code>${escapeHtml(item.code)}</code>`],
        ['剩余', escapeHtml(remaining)],
        ['质保到期', escapeHtml(formatRenewalDateTime(item.warranty_expires_at))],
        ['申请时间', escapeHtml(formatRenewalDateTime(item.requested_at))],
    ].map(([label, value]) => `<div class="renewal-task-line"><span class="renewal-task-label">${label}</span><span>${value}</span></div>`).join('');
}

function renderRenewalCard(item) {
    const remaining = item.remaining_warranty_days !== null && item.remaining_warranty_days !== undefined
        ? `${item.remaining_warranty_days} 天`
        : '-';
    return `
    <div class="renewal-task-card" data-request-id="${item.id}">
        <div class="renewal-task-meta">${renderRenewalDetails(item, remaining)}</div>
        <div class="renewal-task-actions">
            <input type="number" min="${RENEWAL_MIN_DAYS}" max="${RENEWAL_MAX_DAYS}" placeholder="续期天数"
                   class="form-control renewal-days-input"
                   id="renewalModalDays-${item.id}">
            <div class="renewal-task-buttons">
                <button type="button" class="btn btn-sm btn-primary"
                        onclick="handleRenewalExtend(${item.id}, this)">
                    <i data-lucide="calendar-plus-2"></i> 续期
                </button>
                <button type="button" class="btn btn-sm btn-secondary"
                        onclick="handleRenewalIgnore(${item.id}, this)">
                    <i data-lucide="circle-off"></i> 忽略
                </button>
            </div>
        </div>
    </div>
    `;
}
