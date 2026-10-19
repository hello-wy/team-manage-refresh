const DEFAULT_MEMBER_AUTO_KICK_HOURS = 2;
const MEMBER_LIST_TABLE_COLUMNS = Object.freeze({
    modalJoinedMembersTableBody: 6,
    modalInvitedMembersTableBody: 5,
});

function memberListIsCurrent(teamId, requestId) {
    return window.currentTeamId === teamId && requestId === memberListRequestId;
}

function setMemberSyncStatus(message, failed = false) {
    const status = document.getElementById('memberSyncStatus');
    if (!status) return;
    status.textContent = message;
    status.className = failed ? 'text-danger' : 'text-muted';
}

function setMemberListTableMessage(message, failed = false) {
    for (const [id, columns] of Object.entries(MEMBER_LIST_TABLE_COLUMNS)) {
        const body = document.getElementById(id);
        if (body) body.innerHTML = `<tr><td colspan="${columns}" class="${failed ? 'text-danger' : ''}"
            style="text-align: center; padding: 2rem;">${escapeHtml(message)}</td></tr>`;
    }
}

function prepareMemberList(teamId) {
    memberSeatBalance = null;
    updateMemberInviteAvailability();
    setMemberSyncStatus('正在同步最新成员信息，每批读取 10 页...');
    if (renderedMemberTeamId === teamId) return;
    memberExistingEmails = new Set();
    const summary = document.getElementById('memberSeatSummary');
    if (summary) summary.innerHTML = renderMemberSeatSummary(null);
    if (window.lucide) lucide.createIcons();
    setMemberListTableMessage('加载中...');
}

async function loadModalMemberList(teamId, snapshot = null) {
    const requestId = ++memberListRequestId;
    prepareMemberList(teamId);
    try {
        if (snapshot) {
            if (memberListIsCurrent(teamId, requestId)) applyMemberListSnapshot(teamId, snapshot);
            return;
        }
        await synchronizeMemberBatches(teamId, requestId);
    } catch (error) {
        if (!memberListIsCurrent(teamId, requestId)) return;
        memberSeatBalance = null;
        updateMemberInviteAvailability();
        const message = getFriendlyAdminErrorMessage(error.message, 0, 'member');
        setMemberSyncStatus(`成员同步失败：${message}`, true);
        document.getElementById('memberSeatBalanceError').textContent = message;
        if (renderedMemberTeamId !== teamId) setMemberListTableMessage(message, true);
        showToast(message, 'error');
    }
}

async function synchronizeMemberBatches(teamId, requestId) {
    let cursor = null;
    do {
        const result = await apiCall(`/admin/teams/${teamId}/members/sync`, {
            method: 'POST', body: JSON.stringify({cursor}), cache: 'no-store',
        });
        if (!memberListIsCurrent(teamId, requestId)) return;
        if (!result.success || !result.data?.success) {
            throw new Error(result.data?.error || result.error || '成员列表读取失败');
        }
        applyMemberListSnapshot(teamId, result.data);
        const pagination = result.data.pagination;
        if (!pagination?.has_more) return;
        if (!pagination.next_cursor || pagination.next_cursor === cursor) {
            throw new Error('成员同步未返回有效的下一批位置');
        }
        cursor = pagination.next_cursor;
    } while (memberListIsCurrent(teamId, requestId));
}

function applyMemberListSnapshot(teamId, data) {
    renderedMemberTeamId = teamId;
    const members = data.members || [];
    updateTeamMemberCount(teamId, data);
    memberSeatBalance = data.seat_balance || null;
    memberExistingEmails = new Set(members.map(member => (member.email || '').toLowerCase()));
    const autoKick = document.getElementById('memberAutoKickHours');
    if (autoKick) autoKick.value = String(data.member_auto_kick_hours || DEFAULT_MEMBER_AUTO_KICK_HOURS);
    document.getElementById('memberSeatBalanceError').textContent = memberSeatBalance?.success
        ? '' : memberSeatBalance?.error || '余额暂不可用，无法保存或邀请。';
    const summary = document.getElementById('memberSeatSummary');
    if (summary) summary.innerHTML = renderMemberSeatSummary(data.seat_summary);
    renderJoinedMemberList(teamId, members.filter(member => member.status === 'joined'));
    renderInvitedMemberList(teamId, members.filter(member => member.status === 'invited'), data);
    const pagination = data.pagination;
    setMemberSyncStatus(pagination?.has_more
        ? `正在同步：已获取 ${pagination.loaded}/${pagination.total} 位成员，继续读取下一批...`
        : `成员信息已同步，共 ${members.filter(member => member.status === 'joined').length} 位已加入成员`);
    if (window.lucide) lucide.createIcons();
    updateMemberInviteAvailability();
}

function renderJoinedMemberRow(teamId, member) {
    const cached = member.profile_cached_fields;
    return `<tr data-member-email="${escapeHtml(member.email)}" data-joined-at="${escapeHtml(member.added_at || '')}">
        <td>${escapeHtml(member.email || '邮箱未提供')}
            ${member.name ? `<small class="member-name">${escapeHtml(member.name)}</small>` : ''}
            <small class="member-auth-badge">${renderMemberAuthorizationStatus(member)}</small>
            ${cached?.length ? `<small class="member-auth-badge">本次未返回${escapeHtml(cached.join('、'))}，显示上次同步资料</small>` : ''}
            ${member.profile_sync_error ? `<small class="text-danger">${escapeHtml(member.profile_sync_error)}</small>` : ''}
        </td>
        <td><span class="role-badge role-${member.role === 'account-owner' ? 'account-owner' : 'member'}">
            ${member.role === 'account-owner' ? '所有者' : '成员'}</span></td>
        <td>${renderMemberSeatControl(member)}</td>
        <td>${formatDateTime(member.added_at)}</td>
        <td><div class="member-auto-kick-cell">${renderMemberAutoKickTime(member)}
            ${renderMemberAutoKickControl(teamId, member)}</div></td>
        <td style="text-align: right;">${renderMemberAuthorizationActions(teamId, member)}</td>
    </tr>`;
}

function renderJoinedMemberList(teamId, members) {
    const body = document.getElementById('modalJoinedMembersTableBody');
    if (!body) return;
    body.innerHTML = members.length ? members.map(member => renderJoinedMemberRow(teamId, member)).join('')
        : '<tr><td colspan="6" style="text-align: center; padding: 1.5rem; color: var(--text-muted);">暂无已加入成员</td></tr>';
}

function renderInvitedMemberRow(teamId, member) {
    return `<tr>
        <td>${escapeHtml(member.email)}<small class="member-auth-badge">${renderMemberAuthorizationStatus(member)}</small></td>
        <td><span class="role-badge role-member">成员</span></td>
        <td>${renderMemberSeatType(member.seat_type)}</td>
        <td>${formatDateTime(member.added_at)}</td>
        <td style="text-align: right;">${renderMemberAuthorizationActions(teamId, member)}</td>
    </tr>`;
}

function renderInvitedMemberList(teamId, members, data) {
    const body = document.getElementById('modalInvitedMembersTableBody');
    if (!body) return;
    if (data.seat_summary?.invites_complete === false) {
        body.innerHTML = '<tr><td colspan="5" class="text-danger">邀请列表读取失败，请重新打开成员管理重试</td></tr>';
        return;
    }
    body.innerHTML = members.length ? members.map(member => renderInvitedMemberRow(teamId, member)).join('')
        : '<tr><td colspan="5" style="text-align: center; padding: 1.5rem; color: var(--text-muted);">暂无待加入成员</td></tr>';
}
