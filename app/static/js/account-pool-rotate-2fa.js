function showAccountPoolRotatedSecret(email, secret, message) {
    document.getElementById('accountPoolRotate2faEmail').textContent = email;
    document.getElementById('accountPoolRotate2faMessage').textContent = message;
    document.getElementById('accountPoolRotate2faSecret').value = secret;
    showModal('accountPoolRotate2faModal');
}

async function rotateAccountPool2fa(button) {
    const {entryId, email} = button.dataset;
    if (!confirm(`确定更换 ${email} 的 2FA 吗？当前验证器密钥将失效。`)) return;
    button.disabled = true;
    try {
        const response = await fetch(`/admin/account-pool/${entryId}/rotate-2fa`, {
            method: 'POST',
            credentials: 'same-origin',
            cache: 'no-store',
        });
        const result = await response.json();
        if (result.two_factor_secret) {
            showAccountPoolRotatedSecret(email, result.two_factor_secret,
                result.success ? '2FA 已更换并保存。' : result.error);
        }
        if (!response.ok || !result.success) throw new Error(result.error || '更换 2FA 失败');
        window.accountPoolCredentialStore.clear(email);
        await refreshAccountPoolTable();
    } catch (error) {
        showToast(error.message || '更换 2FA 失败', 'error');
    } finally {
        button.disabled = false;
    }
}

window.initAccountPoolRotateButtons = function initAccountPoolRotateButtons() {
    document.querySelectorAll('.account-pool-rotate-2fa-button').forEach(button => {
        if (button.dataset.bound === 'true') return;
        button.dataset.bound = 'true';
        button.addEventListener('click', () => rotateAccountPool2fa(button));
    });
};

document.addEventListener('DOMContentLoaded', () => {
    window.initAccountPoolRotateButtons();
    document.getElementById('accountPoolRotate2faCopy')?.addEventListener('click', async () => {
        const secret = document.getElementById('accountPoolRotate2faSecret').value;
        try {
            await copyToClipboard(secret);
            showToast('2FA 密钥已复制', 'success');
        } catch (error) {
            showToast(error.message || '复制失败', 'error');
        }
    });
});
