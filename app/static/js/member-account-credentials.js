window.memberAccountCredentials = (() => {
    const fields = {
        password: 'memberAuthPassword'
    };

    function totpElement() {
        return document.getElementById('memberAuthTotp');
    }

    function setValues(values = {}) {
        Object.entries(fields).forEach(([key, id]) => {
            const input = document.getElementById(id);
            if (input) input.value = values[key] || '';
        });
        totpElement()?.setSecret(values.two_factor_secret || '');
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
        totpElement()?.showLoading('读取中');
        try {
            const credentials = await window.accountPoolCredentialStore.load(context.email);
            if (memberAuthorizationContext !== context) return;
            setValues(credentials);
            setPlaceholders('未保存');
        } catch (error) {
            if (memberAuthorizationContext !== context) return;
            setValues();
            setPlaceholders(error.message || '读取账号凭据失败');
            totpElement()?.showError(error.message || '读取失败');
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
