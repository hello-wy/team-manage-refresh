window.memberAccountCredentials = (() => {
    const fields = {
        password: 'memberAuthPassword',
        two_factor_secret: 'memberAuthTwoFactorSecret'
    };

    function setValues(values = {}) {
        Object.entries(fields).forEach(([key, id]) => {
            const input = document.getElementById(id);
            if (input) input.value = values[key] || '';
        });
    }

    function setPlaceholders(message) {
        Object.values(fields).forEach(id => {
            const input = document.getElementById(id);
            if (input) input.placeholder = message;
        });
    }

    async function load(context) {
        setValues();
        setPlaceholders('正在从账号号池读取...');
        try {
            const response = await fetch(
                `/admin/account-pool/credentials?email=${encodeURIComponent(context.email)}`,
                {credentials: 'same-origin', cache: 'no-store'}
            );
            const payload = await response.json();
            if (!response.ok || !payload.success) throw new Error(payload.error || '读取账号凭据失败');
            if (memberAuthorizationContext !== context) return;
            setValues(payload.data);
            setPlaceholders('未保存');
        } catch (error) {
            if (memberAuthorizationContext !== context) return;
            setValues();
            setPlaceholders(error.message || '读取账号凭据失败');
        }
    }

    function reset() {
        setValues();
        setPlaceholders('打开成员授权后自动读取');
    }

    async function copy(field, label) {
        const input = document.getElementById(fields[field]);
        const value = input?.value || '';
        if (!value) return showToast(`${label}未保存`, 'warning');
        await copyToClipboard(value);
    }

    return {load, reset, copy};
})();
