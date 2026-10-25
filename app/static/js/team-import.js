function buildManualTeamImportPayload(form) {
    return {
        import_type: 'single',
        access_token: form.accessToken.value.trim(),
        id_token: form.idToken.value.trim() || null,
        refresh_token: form.refreshToken.value.trim() || null,
        session_token: form.sessionToken.value.trim() || null,
        client_id: form.clientId.value.trim() || null,
        email: form.email.value.trim() || null,
        account_id: form.accountId.value.trim() || null,
        pool_type: getCurrentPoolType()
    };
}

const TEAM_CREDENTIAL_SEPARATOR = '----';

function parseTeamImportCredentials(value) {
    const text = value.trim();
    const firstSeparator = text.indexOf(TEAM_CREDENTIAL_SEPARATOR);
    const lastSeparator = text.lastIndexOf(TEAM_CREDENTIAL_SEPARATOR);
    if (firstSeparator < 0 || firstSeparator === lastSeparator || /[\r\n]/.test(text)) {
        throw new Error('请按 邮箱----密码----2FA 密钥 格式粘贴一个账号');
    }
    const email = text.slice(0, firstSeparator).trim();
    const password = text.slice(firstSeparator + TEAM_CREDENTIAL_SEPARATOR.length, lastSeparator);
    const twoFactorSecret = text.slice(lastSeparator + TEAM_CREDENTIAL_SEPARATOR.length).trim();
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
        throw new Error('账号邮箱格式不正确');
    }
    if (!password.trim() || !twoFactorSecret) {
        throw new Error('账号密码和 2FA 密钥不能为空');
    }
    return {email, password, two_factor_secret: twoFactorSecret};
}

function buildCredentialTeamImportPayload(form) {
    return {
        import_type: 'credentials',
        ...parseTeamImportCredentials(form.loginCredentials.value),
        account_id: form.loginAccountId.value.trim() || null,
        pool_type: getCurrentPoolType()
    };
}

async function buildQuickTeamImportPayload() {
    const parsed = await parseOAuthCallbackData();
    const normalized = buildOAuthJsonTemplate(parsed);
    return {
        import_type: 'single',
        access_token: normalized.access_token,
        id_token: normalized.id_token || null,
        refresh_token: normalized.refresh_token || null,
        session_token: null,
        client_id: parsed.client_id || normalized.client_id || null,
        email: normalized.email || null,
        account_id: normalized.account_id || null,
        pool_type: getCurrentPoolType()
    };
}

async function submitTeamImport(payload) {
    const result = await apiCall('/admin/teams/import', {
        method: 'POST',
        body: JSON.stringify(payload)
    });
    if (!result.success || result.data?.success === false) {
        throw new Error(result.error || result.data?.error || '导入失败');
    }
}

async function handleSingleImport(event) {
    event.preventDefault();
    const form = event.target;
    if (form.dataset.importing === 'true') return;

    const quickSection = document.getElementById('oauthQuickSection');
    const credentialSection = document.getElementById('teamCredentialLoginSection');
    const isCredential = !!credentialSection && credentialSection.style.display !== 'none';
    const isQuick = !isCredential && quickSection.style.display !== 'none';
    const activeSection = isCredential ? credentialSection
        : isQuick ? quickSection : document.getElementById('manualTokenSection');
    const submitButton = activeSection.querySelector('button[type="submit"]');
    const buttonText = submitButton.textContent;
    const buttonStates = Array.from(form.querySelectorAll('button'), button => ({
        button, disabled: button.disabled
    }));
    form.dataset.importing = 'true';
    buttonStates.forEach(({button}) => { button.disabled = true; });
    submitButton.textContent = isQuick ? '解析并导入中...' : isCredential ? '登录并导入中...' : '导入中...';

    try {
        const payload = isQuick ? await buildQuickTeamImportPayload()
            : isCredential ? buildCredentialTeamImportPayload(form)
            : buildManualTeamImportPayload(form);
        submitButton.textContent = '导入中...';
        await submitTeamImport(payload);
        showToast('Team 导入成功！', 'success');
        form.reset();
        clearOAuthParsedCache();
        await refreshVisibleAdminData();
    } catch (error) {
        showToast(getFriendlyAdminErrorMessage(error.message || '导入失败', 0, 'import'), 'error');
    } finally {
        delete form.dataset.importing;
        buttonStates.forEach(({button, disabled}) => { button.disabled = disabled; });
        submitButton.textContent = buttonText;
    }
}
