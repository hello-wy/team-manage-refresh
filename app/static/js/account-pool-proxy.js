(() => {
    'use strict';
    const dialog = document.getElementById('accountPoolProxyDialog');
    if (!dialog) return;
    const $ = id => document.getElementById(id);
    const select = $('accountPoolProxySelect');
    const save = $('accountPoolProxySave');
    let context = null;
    let configuration = null;
    let original = '';
    let saving = false;
    let generation = 0;
    let trigger = null;
    const routeValue = route => route.mode === 'proxy' ? `proxy:${route.proxy_id}` : route.mode;
    const routePayload = value => value.startsWith('proxy:')
        ? {mode: 'proxy', proxy_id: Number(value.slice(6))} : {mode: value, proxy_id: null};

    async function api(path, options = {}) {
        const response = await fetch(`/admin/account-pool/${path}`, {
            ...options, credentials: 'same-origin', cache: 'no-store',
            headers: {'Content-Type': 'application/json'}, signal: AbortSignal.timeout(30000),
        });
        let result;
        try { result = await response.json(); } catch { throw new Error('服务响应异常，请稍后重试'); }
        if (!response.ok || !result.success) {
            const detail = result.error || result.detail;
            throw new Error(typeof detail === 'string' ? detail : '代理设置无效，请重新选择');
        }
        return result;
    }

    function error(message = '') {
        $('accountPoolProxyError').textContent = message;
        $('accountPoolProxyError').hidden = !message;
    }

    function lock() {
        select.disabled = saving || !configuration;
        save.disabled = saving || !configuration || select.value === original;
        save.textContent = saving ? '保存中…' : '保存设置';
        dialog.querySelectorAll('[data-close-pool-proxy]').forEach(button => { button.disabled = saving; });
        dialog.setAttribute('aria-busy', String(saving || !configuration));
    }

    function preview() {
        if (!configuration) return;
        let route = routePayload(select.value);
        if (route.mode === 'inherit' && context.entryId !== null) route = configuration.default;
        let label;
        if (route.mode === 'inherit') {
            label = configuration.legacy.enabled
                ? `系统代理 · ${configuration.legacy.address || '尚未配置地址'}` : '服务器直连';
        } else if (route.mode === 'direct') label = '服务器直连';
        else {
            const proxy = configuration.proxies.find(item => item.id === route.proxy_id);
            label = proxy ? `${proxy.name} · ${proxy.address}${proxy.enabled ? '' : '（已停用，请更换）'}`
                : '代理不存在，请重新选择';
        }
        $('accountPoolProxyPreview').textContent = `实际出口：${label}`;
        lock();
    }

    function addOption(value, label, disabled = false) {
        const option = document.createElement('option');
        option.value = value;
        option.textContent = label;
        option.disabled = disabled;
        select.appendChild(option);
    }

    async function open(button, isDefault) {
        if (saving || dialog.open) return;
        trigger = button.closest('details')?.querySelector('summary') || button;
        context = {entryId: isDefault ? null : button.dataset.entryId, email: button.dataset.email || ''};
        configuration = null;
        const current = ++generation;
        $('accountPoolProxyTitle').textContent = isDefault ? '设置默认代理' : '设置账号代理';
        $('accountPoolProxyEmail').textContent = isDefault ? '未单独设置代理的账号使用此出口' : context.email;
        $('accountPoolProxyDescription').textContent = isDefault
            ? '只影响跟随默认出口的账号，已指定代理或直连的账号不受影响。沿用系统设置时，使用系统设置中的代理。'
            : '同一邮箱共用代理设置，适用于登录、验活、授权及 Token 刷新。指定代理不可用时会报错，不会自动切换为直连。';
        $('accountPoolProxyPreview').textContent = '';
        select.replaceChildren();
        addOption('', '正在加载代理…');
        error(); lock();
        button.closest('details')?.removeAttribute('open');
        dialog.showModal();
        try {
            const data = await api(`proxy-options${isDefault ? '' : `?entry_id=${encodeURIComponent(context.entryId)}`}`);
            if (current !== generation || !dialog.open) return;
            configuration = data;
            original = routeValue(isDefault ? data.default : data.account);
            select.replaceChildren();
            addOption('inherit', isDefault ? '沿用系统代理设置' : '跟随默认出口');
            addOption('direct', '服务器直连');
            data.proxies.forEach(proxy => addOption(`proxy:${proxy.id}`,
                `${proxy.name}${proxy.enabled ? '' : '（已停用）'}`, !proxy.enabled));
            if (original.startsWith('proxy:') && !data.proxies.some(proxy => `proxy:${proxy.id}` === original)) {
                addOption(original, '原代理已不存在，请更换', true);
            }
            select.value = original;
            preview();
            select.focus();
        } catch (failure) {
            if (current !== generation || !dialog.open) return;
            error(failure.message || '读取代理失败，请关闭后重试');
            dialog.setAttribute('aria-busy', 'false');
        }
    }

    function close() {
        if (!saving) dialog.close();
    }

    document.addEventListener('click', event => {
        const button = event.target.closest('.account-pool-proxy-button, #accountPoolDefaultProxy');
        if (button) open(button, button.id === 'accountPoolDefaultProxy');
    });
    dialog.querySelectorAll('[data-close-pool-proxy]').forEach(button => button.addEventListener('click', close));
    dialog.addEventListener('cancel', event => { if (saving) event.preventDefault(); });
    dialog.addEventListener('close', () => {
        generation++;
        configuration = null;
        trigger?.focus();
    });
    select.addEventListener('change', () => { error(); preview(); });
    $('accountPoolProxyForm').addEventListener('submit', async event => {
        event.preventDefault();
        if (saving || !configuration || select.value === original) return;
        const target = {...context};
        saving = true; error(); lock();
        try {
            await api(target.entryId === null ? 'proxy-default' : `${encodeURIComponent(target.entryId)}/proxy`, {
                method: 'PUT', body: JSON.stringify(routePayload(select.value)),
            });
        } catch (failure) {
            error(failure.message || '保存代理失败，请重试');
            return;
        } finally { saving = false; lock(); }
        dialog.close();
        window.showToast?.(target.entryId === null ? '默认代理已保存' : '账号代理已保存', 'success');
        try {
            await window.refreshAccountPoolTable?.({refreshQuotas: false});
            if (target.entryId !== null) {
                [...document.querySelectorAll('.account-pool-proxy-button')]
                    .find(button => button.dataset.entryId === target.entryId)?.focus();
            } else trigger?.focus();
        } catch { window.showToast?.('代理已保存，请刷新列表查看最新出口', 'warning'); }
    });
})();
