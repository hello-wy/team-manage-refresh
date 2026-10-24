function renderMemberSeatSummary(summary) {
    return [['standard', '标准', 'users'], ['premium', '高级', 'crown']]
        .map(([key, label, icon]) => {
            const quota = memberSeatQuota(key);
            const details = [
                `已购 ${quota ? quota.paid : '—'}`,
                `已加入 ${summary ? Number(summary.joined[key]) || 0 : '—'}`,
                summary?.invites_complete ? (summary.invited[key] ? `邀请中 ${Number(summary.invited[key]) || 0}` : '') : '邀请数未知',
                quota?.reserved ? `待确认 ${quota.reserved}` : '',
            ].filter(Boolean).join(' · ');
            const empty = quota && !quota.remaining ? ' is-empty' : '';
            return `<div class="member-seat-chip member-seat-chip-${key}${empty}">
                <i data-lucide="${icon}" aria-hidden="true"></i>
                <strong>${label}剩余 ${quota ? quota.remaining : '—'}</strong>
                <small>${details}</small></div>`;
        }).join('');
}

