/* All purchased seats are displayed inside Team member management. */
(() => {
    function renderSeats(layout) {
        const groups = layout.map(group => {
            const caption = group.paid === null ? '数量待同步'
                : group.paid === 0 ? '未购买' : `${group.remaining} 空闲 / ${group.paid} 席`;
            const slots = group.slots.map((slot, i) => {
                const available = slot.state === 'available';
                const icon = available ? '+' : slot.state === 'reserved' ? '·' : slot.label[0].toUpperCase();
                return `<button type="button" class="team-seat-slot is-${slot.state} ${available ? 'btn-invite-seat' : 'btn-seat-members'}"
                    data-member-tab="${slot.state === 'invited' ? 'invited' : 'joined'}" data-seat-type="${escapeHtml(group.seat_type)}"
                    title="${escapeHtml(`${group.label} · ${slot.label} · ${slot.detail}`)}"
                    aria-label="${escapeHtml(`${group.label} ${i + 1}：${slot.label}，${slot.detail}`)}">
                    <span class="team-seat-avatar" aria-hidden="true">${escapeHtml(icon)}</span>
                    <span class="team-seat-slot-copy"><span class="team-seat-slot-name">${escapeHtml(slot.label)}</span>
                    <span class="team-seat-slot-detail">${escapeHtml(slot.detail)}</span></span>
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
        const board = document.getElementById('memberSeatsBoard');
        if (board) board.innerHTML = '<p class="member-seats-message">正在同步完整席位信息...</p>';
    }

    function update(data) {
        const board = document.getElementById('memberSeatsBoard');
        if (!board) return;
        if (data.pagination?.has_more) return reset();
        board.innerHTML = data.seat_layout ? renderSeats(data.seat_layout)
            : '<p class="member-seats-message">席位信息暂不可用，请刷新重试。</p>';
    }

    function fail(message) {
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

    window.memberSeats = {reset, update, fail};
})();
