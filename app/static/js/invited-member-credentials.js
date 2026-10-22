window.invitedMemberCredentials = (() => {
    let snapshot = null;
    let busy = false;

    function updateButton() {
        const button = document.getElementById('copyInvitedMemberCredentialsBtn');
        const label = document.getElementById('copyInvitedMemberCredentialsLabel');
        if (button) button.disabled = busy || !snapshot?.emails.length;
        if (label) label.textContent = busy ? '正在读取账号…' : '批量复制账号';
    }

    function reset() {
        snapshot = null;
        updateButton();
    }

    function update(teamId, members, data) {
        const complete = data.seat_summary?.invites_complete !== false && !data.pagination?.has_more;
        snapshot = complete ? Object.freeze({
            teamId,
            requestId: memberListRequestId,
            emails: Object.freeze(members.map(member => member.email)),
        }) : null;
        updateButton();
    }

    async function readAccount(email) {
        try {
            const credentials = await window.accountPoolCredentialStore.load(email, {fresh: true});
            return {email, password: credentials.password, secret: credentials.two_factor_secret};
        } catch (error) {
            throw new Error(`${email}：${error.message}`);
        }
    }

    async function copy() {
        if (busy) return;
        const current = snapshot;
        if (!current?.emails.length) return showToast('暂无可复制的邀请中账号', 'warning');
        busy = true;
        updateButton();
        try {
            const accounts = await Promise.all(current.emails.map(readAccount));
            if (snapshot !== current || window.currentTeamId !== current.teamId
                || memberListRequestId !== current.requestId) {
                throw new Error('成员列表已变化，请重新点击批量复制');
            }
            const text = accounts.map(account =>
                `${account.email}----${account.password || ''}----${account.secret || ''}`).join('\n');
            const copied = await copyToClipboard(text);
            const missing = accounts.filter(account => !account.password || !account.secret);
            if (copied && missing.length) {
                showToast(`已复制 ${accounts.length} 个账号；${missing.length} 个账号未保存密码或 2FA，对应字段留空`, 'warning');
            }
        } catch (error) {
            showToast(`批量复制失败：${error.message}`, 'error');
        } finally {
            busy = false;
            updateButton();
        }
    }

    return Object.freeze({reset, update, copy});
})();
