/* All purchased seats are displayed inside Team member management. */
(() => {
    let currentLayout = null;
    let loadedAt = 0;

    function timeLabel(value) {
        return String(value).replace('T', ' ').slice(0, 16);
    }

    function remainingTime(slot, value) {
        // 服务端时间均为同一时区的本地时间；按相对差值校准，避免浏览器时区影响。
        return Date.parse(value + 'Z') - Date.parse(slot.server_now + 'Z') - (Date.now() - loadedAt);
    }

    function durationLabel(milliseconds) {
        const minutes = Math.max(0, Math.ceil(milliseconds / 60000));
        return minutes >= 1440 ? `${Math.floor(minutes / 1440)} 天 ${Math.floor(minutes % 1440 / 60)} 小时`
            : minutes >= 60 ? `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分` : `${minutes} 分`;
    }

    function timeDetails(slot) {
        if (['available', 'reserved'].includes(slot.state) || slot.seat_status === 'owner') return '';
        const rows = [];
        if (slot.state !== 'invited') {
            if (slot.warranty_expires_at) {
                const remaining = remainingTime(slot, slot.warranty_expires_at);
                rows.push(remaining > 0 ? `质保剩余 ${durationLabel(remaining)}` : '质保已结束');
                rows.push(`质保截止 ${timeLabel(slot.warranty_expires_at)}`);
            }
            if (slot.auto_kick_at) {
                if (slot.seat_status === 'timed') rows.push(`本轮剩余 ${durationLabel(remainingTime(slot, slot.auto_kick_at))}`);
                rows.push(`${slot.auto_kick_at === slot.team_expires_at ? 'Team 到期下线' : '预计下线'} ${timeLabel(slot.auto_kick_at)}`);
            }
        }
        if (slot.team_expires_at && slot.auto_kick_at !== slot.team_expires_at) rows.push(`Team 到期 ${timeLabel(slot.team_expires_at)}`);
        return rows.map(row => `<span class="team-seat-time">${escapeHtml(row)}</span>`).join('');
    }

    function renderSeats(layout) {
        const groups = layout.map(group => {
            const caption = group.paid === null ? '数量待同步'
                : group.paid === 0 ? '未购买' : `${group.remaining} 空闲 / ${group.paid} 席`;
            const slots = group.slots.map((slot, i) => {
                const available = slot.state === 'available';
                const icon = available ? '+' : slot.state === 'reserved' ? '·' : slot.label[0].toUpperCase();
                let label = slot.status_label || slot.detail;
                let status = slot.seat_status || slot.state;
                const reason = slot.status_reason || slot.detail;
                if (available) label = '未邀请';
                if (slot.state === 'reserved') label = '待同步';
                if (status === 'warranty' && remainingTime(slot, slot.warranty_expires_at) <= 0) {
                    label = '质保已结束'; status = 'protected';
                }
                if (slot.state === 'joined' && slot.auto_kick_at && remainingTime(slot, slot.auto_kick_at) <= 0) {
                    label = '到期待下线'; status = 'overdue';
                }
                return `<button type="button" class="team-seat-slot is-${slot.state} ${available ? 'btn-invite-seat' : 'btn-seat-members'}"
                    data-member-tab="${slot.state === 'invited' ? 'invited' : 'joined'}" data-seat-type="${escapeHtml(group.seat_type)}"
                    title="${escapeHtml(`${group.label} · ${slot.label} · ${reason}`)}"
                    aria-label="${escapeHtml(`${group.label} ${i + 1}：${slot.label}，${label}，${reason}`)}">
                    <span class="team-seat-avatar" aria-hidden="true">${escapeHtml(icon)}</span>
                    <span class="team-seat-slot-copy"><span class="team-seat-slot-name">${escapeHtml(slot.label)}</span>
                    <span class="team-seat-status status-${escapeHtml(status)}">${escapeHtml(label)}</span>
                    <span class="team-seat-slot-detail">${escapeHtml(reason)}</span>${timeDetails(slot)}</span>
                    ${available ? '<span class="team-seat-invite-label">邀请 <span aria-hidden="true">↗</span></span>' : ''}</button>`;
            }).join('');
            return `<section class="team-seat-group is-${group.kind}" aria-label="${group.label}">
                <div class="team-seat-group-head"><span class="team-seat-kind">${group.label}</span>
                <span class="team-seat-caption">${caption}</span></div>
                ${slots ? `<div class="team-seat-grid">${slots}</div>` : ''}</section>`;
        }).join('');
        return `<div class="team-seat-board">${groups}</div>`;
    }

    function reset() {
        currentLayout = null;
        const board = document.getElementById('memberSeatsBoard');
        if (board) board.innerHTML = '<p class="member-seats-message">正在同步完整席位信息...</p>';
    }

    function update(data) {
        const board = document.getElementById('memberSeatsBoard');
        if (!board) return;
        if (data.pagination?.has_more) return reset();
        currentLayout = data.seat_layout;
        loadedAt = Date.now();
        board.innerHTML = data.seat_layout ? renderSeats(data.seat_layout)
            : '<p class="member-seats-message">席位信息暂不可用，请刷新重试。</p>';
    }

    function fail(message) {
        currentLayout = null;
        const board = document.getElementById('memberSeatsBoard');
        if (board) board.innerHTML = `<p class="member-seats-message">席位同步失败：${escapeHtml(message)}。请点击刷新重试。</p>`;
    }

    document.addEventListener('click', event => {
        const invite = event.target.closest('#memberPanelSeats .btn-invite-seat');
        if (invite) {
            const seatType = invite.dataset.seatType;
            if (!['default', 'premium'].includes(seatType) || !canAllocateMemberSeat(seatType)) return;
            document.getElementById('inviteSeatType').value = seatType;
            document.getElementById('memberEmails').value = '';
            document.getElementById('memberPoolEmail').value = '';
            selectMemberTab('invite');
            updateMemberInviteAvailability();
            document.getElementById('memberEmails').focus({preventScroll: true});
        }
        const member = event.target.closest('#memberPanelSeats .btn-seat-members');
        if (member) selectMemberTab(member.dataset.memberTab);
    });

    if (window.setInterval) window.setInterval(() => {
        const board = document.getElementById('memberSeatsBoard');
        if (currentLayout && board && board.getClientRects().length) board.innerHTML = renderSeats(currentLayout);
    }, 60000);

    window.memberSeats = {reset, update, fail};
})();
