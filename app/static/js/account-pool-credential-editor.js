function accountPoolCredentialControls() {
    return {
        form: document.getElementById('accountPoolCredentialForm'),
        fields: document.getElementById('accountPoolCredentialFields'),
        status: document.getElementById('accountPoolCredentialStatus'),
        password: document.getElementById('accountPoolCredentialPassword'),
        secret: document.getElementById('accountPoolCredentialTwoFactor'),
    };
}

function resetAccountPoolCredentialEditor() {
    const {form, fields, status, password, secret} = accountPoolCredentialControls();
    password.value = '';
    secret.value = '';
    password.defaultValue = '';
    secret.defaultValue = '';
    form.hidden = true;
    fields.disabled = true;
    delete form.dataset.requestId;
    delete form.dataset.loaded;
    status.textContent = '';
}

async function loadAccountPoolCredentialEditor({entryId, email, signal}) {
    resetAccountPoolCredentialEditor();
    const {form, fields, status, password, secret} = accountPoolCredentialControls();
    const requestId = String(Number(form.dataset.requestSequence || 0) + 1);
    form.dataset.requestSequence = requestId;
    form.dataset.requestId = requestId;
    form.dataset.entryId = String(entryId);
    form.hidden = false;
    status.textContent = '正在读取密码和 2FA…';
    try {
        const credentials = await window.accountPoolCredentialStore.load(email, {fresh: true});
        if (signal.aborted || form.dataset.requestId !== requestId) return;
        password.value = credentials.password;
        secret.value = credentials.two_factor_secret;
        password.defaultValue = credentials.password;
        secret.defaultValue = credentials.two_factor_secret;
        form.dataset.loaded = 'true';
        fields.disabled = false;
        status.textContent = credentials.password || credentials.two_factor_secret
            ? '补充或修改后点击保存；留空的项目保持原值。'
            : '尚未保存密码和 2FA，请填写后保存；支持先保存其中一项。';
    } catch (error) {
        if (signal.aborted || form.dataset.requestId !== requestId) return;
        status.textContent = error.message || '读取账号凭据失败，请关闭后重新打开。';
    }
}

function accountPoolCredentialChanges({password, secret}) {
    const twoFactorSecret = secret.value.trim();
    return {
        ...(password.value && password.value !== password.defaultValue
            ? {password: password.value} : {}),
        ...(twoFactorSecret && twoFactorSecret !== secret.defaultValue
            ? {two_factor_secret: twoFactorSecret} : {}),
    };
}

async function submitAccountPoolCredentialForm(event) {
    event.preventDefault();
    const controls = accountPoolCredentialControls();
    const {form, fields, status} = controls;
    if (form.dataset.loaded !== 'true' || fields.disabled) return;
    const payload = accountPoolCredentialChanges(controls);
    if (!Object.keys(payload).length) return showToast('请修改密码或 2FA 后再保存', 'warning');
    const {entryId, requestId} = form.dataset;
    fields.disabled = true;
    status.textContent = '正在保存…';
    try {
        const response = await fetch(`/admin/account-pool/${entryId}/credentials`, {
            method: 'PATCH', credentials: 'same-origin',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload),
        });
        const result = await response.json();
        if (!response.ok || !result.success) throw new Error(result.error || '保存账号凭据失败');
        window.accountPoolCredentialStore.clear(result.data.email);
        showToast(result.message, 'success');
        if (form.dataset.requestId === requestId) closeAccountPoolDetails();
        await refreshAccountPoolTable();
    } catch (error) {
        showToast(error.message || '保存账号凭据失败', 'error');
        if (form.dataset.requestId === requestId) status.textContent = error.message;
    } finally {
        if (form.dataset.requestId === requestId) fields.disabled = false;
    }
}

document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('accountPoolCredentialForm').addEventListener(
        'submit', submitAccountPoolCredentialForm
    );
});
