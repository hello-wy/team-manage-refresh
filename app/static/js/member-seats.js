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

    function cardStatus(slot, kind) {
        if (slot.state === 'available') return {status: 'available', label: '未邀请'};
        if (slot.state === 'reserved') return {status: 'reserved', label: '待同步'};
        if (slot.seat_status === 'owner') return {status: 'owner', label: '所有者'};
        if (slot.state === 'invited') return {status: 'invited', label: '待确认'};
        if (kind === 'premium' && slot.auto_kick_enabled === false) {
            return {status: 'retained', label: '不自动下线'};
        }
        const deadline = kind === 'standard' ? slot.team_expires_at : slot.auto_kick_at;
        return deadline && remainingTime(slot, deadline) <= 0
            ? {status: 'overdue', label: '待下线'} : {status: 'timed', label: '使用中'};
    }

    function timeDetails(slot, kind) {
        if (['available', 'reserved'].includes(slot.state) || slot.seat_status === 'owner') return '';
        const rows = [];
        if (kind === 'standard') {
            rows.push(`到期 ${slot.team_expires_at ? timeLabel(slot.team_expires_at) : '—'}`);
        } else if (slot.state !== 'invited') {
            const joinedAt = slot.joined_at || slot.added_at;
            rows.push(`加入 ${joinedAt ? timeLabel(joinedAt) : '—'}`);
            rows.push(`退出 ${slot.auto_kick_enabled !== false && slot.auto_kick_at ? timeLabel(slot.auto_kick_at) : '—'}`);
        }
        return rows.map(row => `<span class="team-seat-time">${escapeHtml(row)}</span>`).join('');
    }

    function rotationCount(slot, kind) {
        if (kind !== 'premium' || slot.state === 'reserved' || slot.state === 'available' || slot.seat_status === 'owner') return '';
        const round = Math.max(1, Number(slot.rotation_round) || 1);
        const count = Math.max(0, round - (slot.state === 'invited' ? 1 : 0));
        const limit = Math.max(0, Number(slot.rotation_rounds) || 0);
        return `<span class="team-seat-rotation-count">已轮转 ${limit ? `${count}/${limit}` : count} 个账号</span>`;
    }

    function renderSeats(layout) {
        const groups = layout.map(group => {
            const caption = group.paid === null ? '数量待同步'
                : group.paid === 0 ? '未购买' : `${group.remaining} 空闲 / ${group.paid} 席`;
            const slots = group.slots.map((slot, i) => {
                const available = slot.state === 'available';
                const icon = available ? '+' : slot.state === 'reserved' ? '·' : slot.label[0].toUpperCase();
                const {label, status} = cardStatus(slot, group.kind);
                return `<button type="button" class="team-seat-slot is-${slot.state} ${available ? 'btn-invite-seat' : 'btn-seat-members'}"
                    data-member-tab="${slot.state === 'invited' ? 'invited' : 'joined'}" data-seat-type="${escapeHtml(group.seat_type)}"
                    title="${escapeHtml(`${group.label} · ${slot.label} · ${label}`)}"
                    aria-label="${escapeHtml(`${group.label} ${i + 1}：${slot.label}，${label}`)}">
                    <span class="team-seat-avatar" aria-hidden="true">${escapeHtml(icon)}</span>
                    <span class="team-seat-slot-copy"><span class="team-seat-slot-name">${escapeHtml(slot.label)}</span>
                    <span class="team-seat-status status-${escapeHtml(status)}">${escapeHtml(label)}</span>
                    ${rotationCount(slot, group.kind)}
                    ${timeDetails(slot, group.kind)}</span>
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
