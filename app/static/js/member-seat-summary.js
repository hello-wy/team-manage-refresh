function renderMemberSeatSummary(summary) {
    return [['standard', '标准席位', 'users'], ['premium', '高级席位', 'crown']]
        .map(([key, label, icon]) => {
            const quota = memberSeatQuota(key);
            return `<div class="member-seat-card member-seat-card-${key}">
                <span class="member-overview-icon" aria-hidden="true"><i data-lucide="${icon}"></i></span>
                <div class="member-seat-card-copy"><strong>${label} · 剩余 ${quota ? quota.remaining : '暂不可用'}</strong>
                <small>已购 ${quota ? quota.paid : '—'} · 已加入 ${summary ? Number(summary.joined[key]) || 0 : '—'}${summary?.invites_complete ? (summary.invited[key] ? ` · 邀请中 ${Number(summary.invited[key]) || 0}` : '') : ' · 邀请数未知'}${quota?.reserved ? ` · 待确认 ${quota.reserved}` : ''}</small>
                </div></div>`;
        }).join('');
}

