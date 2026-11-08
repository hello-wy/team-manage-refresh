(() => {
    'use strict';
    const root = document.getElementById('proxyPage');
    if (!root) return;
    const $ = id => document.getElementById(id);
    const state = {proxies: [], accounts: [], default: {mode: 'inherit', proxy_id: null}, legacy: {}, tab: 'nodes', loaded: false};
    const pending = new Map();
    const testing = new Set();
    let editing = null;
    let testingAll = false;
    let savingEditor = false;
    let defaultSelection = null;
    let savingDefault = false;
    const savingAccounts = new Set();
    const icons = () => window.lucide?.createIcons();
    const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char]));
    const routeValue = route => route.mode === 'proxy' ? `proxy:${route.proxy_id}` : route.mode;
    const routePayload = value => value.startsWith('proxy:') ? {mode: 'proxy', proxy_id: Number(value.slice(6))} : {mode: value, proxy_id: null};
    const time = value => value ? value.replace('T', ' ').slice(0, 19) : '尚未检测';
    const toast = (message, error = false) => window.showToast?.(message, error ? 'error' : 'success');

    async function api(path, options = {}) {
        const response = await fetch(`/admin/proxies${path}`, {
            ...options, headers: {'Content-Type': 'application/json'}, cache: 'no-store',
        });
        let data;
        try { data = await response.json(); } catch { throw new Error('服务响应异常，请稍后重试'); }
        if (!response.ok || data.success === false) {
            const detail = data.error || data.detail;
            throw new Error(typeof detail === 'string' ? detail : '提交内容无效，请检查后重试');
        }
        return data;
    }

    function options(selected, isDefault = false) {
        const choices = [{value: 'inherit', label: isDefault ? '沿用系统代理设置' : '使用默认出口'}, {value: 'direct', label: '服务器直连'}];
        state.proxies.forEach(proxy => choices.push({value: `proxy:${proxy.id}`, label: `${proxy.name}${proxy.enabled ? '' : '（已停用）'}`, disabled: !proxy.enabled}));
        return choices.map(choice => `<option value="${esc(choice.value)}"${selected === choice.value ? ' selected' : ''}${choice.disabled ? ' disabled' : ''}>${esc(choice.label)}</option>`).join('');
    }

    function effective(route) {
        if (route.mode === 'inherit') route = state.default;
        if (route.mode === 'inherit') return {label: state.legacy.enabled ? '系统代理' : '服务器直连', address: state.legacy.enabled ? state.legacy.address : '当前没有使用代理'};
        if (route.mode === 'direct') return {label: '服务器直连', address: '使用服务器网络出口'};
        const proxy = state.proxies.find(item => item.id === route.proxy_id);
        return {label: proxy ? proxy.name : '代理不存在', address: proxy?.enabled ? [proxy.exit_ip, proxy.country, proxy.city].filter(Boolean).join(' · ') || proxy.test_message || '等待检测出口' : '节点已停用，账号请求将被阻止', error: !proxy?.enabled || ['offline', 'error'].includes(proxy?.test_status)};
    }

    function metrics() {
        $('proxyTotal').textContent = state.proxies.length;
        $('proxyOnline').textContent = state.proxies.filter(proxy => proxy.enabled && proxy.test_status === 'online').length;
        $('proxyBound').textContent = state.accounts.filter(account => account.mode === 'proxy').length;
        $('proxyNodeCount').textContent = state.proxies.length;
        $('proxyAccountCount').textContent = state.accounts.length;
    }

    function renderDefault() {
        $('proxyDefaultSelect').innerHTML = options(defaultSelection ?? routeValue(state.default), true);
        $('proxyDefaultSelect').disabled = savingDefault;
        $('proxyDefaultSave').disabled = savingDefault;
        const exit = effective({mode: 'inherit'});
        $('proxyDefaultHint').textContent = `${exit.label} · ${exit.address}`;
    }

    function renderNodes() {
        const needle = $('proxySearch').value.trim().toLowerCase();
        const proxies = state.proxies.filter(proxy => [proxy.name, proxy.address, proxy.exit_ip, proxy.country, proxy.region, proxy.city].join(' ').toLowerCase().includes(needle));
        $('proxyNodesBody').innerHTML = proxies.map(proxy => {
            const status = proxy.enabled ? proxy.test_status : 'disabled';
            const busy = testing.has(proxy.id);
            const labels = {online: '可用', offline: '连接失败', error: '检测异常', untested: '未检测', disabled: '已停用'};
            return `<tr data-proxy-id="${proxy.id}">
                <td><div class="proxy-cell"><strong>${esc(proxy.name)}</strong><code>${esc(proxy.address)}</code></div></td>
                <td><div class="proxy-cell"><span class="proxy-ip">${esc(proxy.exit_ip || '—')}</span><small>${esc([proxy.country, proxy.region, proxy.city].filter(Boolean).join(' · ') || '地理信息待检测')}</small></div></td>
                <td><span class="proxy-latency">${proxy.latency_ms == null ? '—' : `${proxy.latency_ms} ms`}</span></td>
                <td><div class="proxy-cell"><span class="proxy-badge ${esc(status)}" title="${esc(proxy.test_message || '')}">${busy ? '检测中…' : esc(labels[status] || '未检测')}</span><small>${esc(time(proxy.tested_at))}</small>${proxy.test_message && (status === 'offline' || status === 'error' || !proxy.country) ? `<small>${esc(proxy.test_message)}</small>` : ''}</div></td>
                <td><span class="proxy-latency">${proxy.account_count}</span></td>
                <td><div class="proxy-row-actions"><button type="button" class="btn btn-secondary btn-sm" data-action="test"${busy ? ' disabled' : ''}>${busy ? '检测中' : '检测'}</button><button type="button" class="proxy-icon-button" data-action="edit" aria-label="编辑 ${esc(proxy.name)}" title="编辑"><i data-lucide="pencil"></i></button><button type="button" class="proxy-icon-button" data-action="toggle" aria-label="${proxy.enabled ? '停用' : '启用'} ${esc(proxy.name)}" title="${proxy.enabled ? '停用' : '启用'}"><i data-lucide="${proxy.enabled ? 'pause' : 'play'}"></i></button><button type="button" class="proxy-icon-button" data-action="delete" aria-label="删除 ${esc(proxy.name)}" title="删除"><i data-lucide="trash-2"></i></button></div></td>
            </tr>`;
        }).join('');
        $('proxyNodesEmpty').hidden = proxies.length > 0;
        $('proxyNodesEmpty').querySelector('h3').textContent = state.proxies.length ? '没有匹配的代理' : '添加第一个代理节点';
        $('proxyTestAll').disabled = testingAll || !state.proxies.some(proxy => proxy.enabled);
        icons();
    }

    function renderAccounts() {
        const needle = $('proxySearch').value.trim().toLowerCase();
        const accounts = state.accounts.filter(account => account.email.toLowerCase().includes(needle));
        $('proxyAccountsBody').innerHTML = accounts.map(account => {
            const value = pending.get(account.email) || routeValue(account);
            const exit = effective(account);
            const status = account.status === 'alive' ? '验证正常' : account.status ? '验活异常' : '未检测';
            return `<tr data-email="${esc(account.email)}" data-dirty="${pending.has(account.email)}">
                <td><strong>${esc(account.email)}</strong></td><td><div class="proxy-cell"><span>${esc(account.source)}</span><small>${esc(status)}</small></div></td>
                <td><select class="form-control" aria-label="${esc(account.email)} 的登录出口"${savingAccounts.has(account.email) ? ' disabled' : ''}>${options(value)}</select></td>
                <td><div class="proxy-cell"><strong${exit.error ? ' class="proxy-form-error"' : ''}>${esc(exit.label)}</strong><small>${esc(exit.address)}</small></div></td>
                <td class="proxy-actions-heading"><button class="btn btn-secondary btn-sm" type="button" data-action="save-route"${pending.has(account.email) && !savingAccounts.has(account.email) ? '' : ' disabled'}>保存</button></td>
            </tr>`;
        }).join('');
        $('proxyAccountsEmpty').hidden = accounts.length > 0;
    }

    function render() { metrics(); renderDefault(); renderNodes(); renderAccounts(); }

    async function load() {
        $('proxyPageMessage').hidden = false;
        $('proxyPageMessage').classList.remove('error');
        $('proxyPageMessage').textContent = '正在加载代理与账号…';
        $('proxyRetry').hidden = true;
        try {
            const data = await api('/data');
            Object.assign(state, {proxies: data.proxies, accounts: data.accounts, default: data.default, legacy: data.legacy, loaded: true});
            $('proxyPageMessage').hidden = true;
            render();
        } catch (error) {
            $('proxyPageMessage').textContent = error.message;
            $('proxyPageMessage').classList.add('error');
            $('proxyRetry').hidden = false;
        }
    }

    function switchTab(tab) {
        state.tab = tab;
        for (const name of ['nodes', 'accounts']) {
            const button = $(name === 'nodes' ? 'proxyNodesTab' : 'proxyAccountsTab');
            button.setAttribute('aria-selected', String(name === tab));
            button.tabIndex = name === tab ? 0 : -1;
            $(name === 'nodes' ? 'proxyNodesPanel' : 'proxyAccountsPanel').hidden = name !== tab;
        }
        $('proxySearch').value = '';
        $('proxySearch').placeholder = tab === 'nodes' ? '搜索节点名称、地址…' : '搜索账号邮箱…';
        $('proxyTestAll').hidden = tab !== 'nodes';
        if (state.loaded) { renderNodes(); renderAccounts(); }
    }

    function editor(proxy) {
        editing = proxy?.id ?? null;
        $('proxyEditorTitle').textContent = proxy ? '编辑代理' : '新增代理';
        $('proxyName').value = proxy?.name || '';
        $('proxyEnabled').checked = proxy ? proxy.enabled : true;
        $('proxyUrl').value = '';
        $('proxyUrl').type = 'password';
        $('proxyUrl').required = !proxy;
        $('proxyReveal').setAttribute('aria-pressed', 'false');
        $('proxyUrlHint').textContent = proxy ? `留空保留当前地址及认证：${proxy.address}` : '支持 http://、https://、socks5://、socks5h://；账号密码中的特殊字符请 URL 编码。';
        $('proxyEditorError').hidden = true;
        $('proxyEditor').showModal();
        $('proxyName').focus();
    }

    function closeEditor() {
        if (savingEditor) return;
        $('proxyEditor').close();
        $('proxyUrl').value = '';
    }

    async function testProxy(id) {
        if (testing.has(id)) return;
        testing.add(id); renderNodes();
        try {
            const data = await api(`/nodes/${id}/test`, {method: 'POST'});
            state.proxies = state.proxies.map(proxy => proxy.id === id ? data.proxy : proxy);
            if (!testingAll) toast(data.proxy.test_message || '检测完成', data.proxy.test_status !== 'online');
        } catch (error) { toast(error.message, true); }
        finally { testing.delete(id); metrics(); renderNodes(); renderAccounts(); renderDefault(); }
    }

    $('proxyNodesTab').addEventListener('click', () => switchTab('nodes'));
    $('proxyAccountsTab').addEventListener('click', () => switchTab('accounts'));
    root.querySelector('[role="tablist"]').addEventListener('keydown', event => {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        switchTab(event.key === 'Home' ? 'nodes' : event.key === 'End' ? 'accounts' : state.tab === 'nodes' ? 'accounts' : 'nodes');
        $(state.tab === 'nodes' ? 'proxyNodesTab' : 'proxyAccountsTab').focus();
    });
    $('proxySearch').addEventListener('input', () => state.tab === 'nodes' ? renderNodes() : renderAccounts());
    $('proxyAdd').addEventListener('click', () => editor());
    $('proxyEmptyAdd').addEventListener('click', () => editor());
    $('proxyRetry').addEventListener('click', load);
    $('proxyEditorClose').addEventListener('click', closeEditor);
    $('proxyEditorCancel').addEventListener('click', closeEditor);
    $('proxyEditor').addEventListener('cancel', event => { if (savingEditor) event.preventDefault(); });
    $('proxyEditor').addEventListener('close', () => { $('proxyUrl').value = ''; });
    $('proxyReveal').addEventListener('click', () => {
        const visible = $('proxyUrl').type === 'password';
        $('proxyUrl').type = visible ? 'text' : 'password';
        $('proxyReveal').setAttribute('aria-pressed', String(visible));
        $('proxyReveal').setAttribute('aria-label', visible ? '隐藏输入的代理地址' : '显示输入的代理地址');
    });

    $('proxyEditorForm').addEventListener('submit', async event => {
        event.preventDefault();
        if (savingEditor) return;
        savingEditor = true; $('proxyEditorSave').disabled = true; $('proxyEditorError').hidden = true;
        try {
            const data = await api(editing ? `/nodes/${editing}` : '/nodes', {
                method: editing ? 'PUT' : 'POST', body: JSON.stringify({name: $('proxyName').value.trim(), url: $('proxyUrl').value.trim() || null, enabled: $('proxyEnabled').checked}),
            });
            state.proxies = editing ? state.proxies.map(proxy => proxy.id === editing ? data.proxy : proxy) : [...state.proxies, data.proxy];
            savingEditor = false; closeEditor(); render(); toast('代理已保存');
        } catch (error) { $('proxyEditorError').textContent = error.message; $('proxyEditorError').hidden = false; }
        finally { savingEditor = false; $('proxyEditorSave').disabled = false; }
    });

    $('proxyNodesBody').addEventListener('click', async event => {
        const button = event.target.closest('button[data-action]');
        if (!button) return;
        const id = Number(button.closest('tr').dataset.proxyId);
        const proxy = state.proxies.find(item => item.id === id);
        if (!proxy) return;
        if (button.dataset.action === 'test') return testProxy(id);
        if (button.dataset.action === 'edit') return editor(proxy);
        const isDelete = button.dataset.action === 'delete';
        if ((isDelete || proxy.enabled) && !await window.confirmPageAction(isDelete ? `删除代理「${proxy.name}」？仍被使用的代理无法删除。` : `停用代理「${proxy.name}」？使用此节点的账号将无法登录，直到重新启用或更换出口。`)) return;
        button.disabled = true;
        try {
            const data = await api(`/nodes/${id}`, {method: isDelete ? 'DELETE' : 'PUT', ...(isDelete ? {} : {body: JSON.stringify({name: proxy.name, enabled: !proxy.enabled})})});
            state.proxies = isDelete ? state.proxies.filter(item => item.id !== id) : state.proxies.map(item => item.id === id ? data.proxy : item);
            render(); toast(isDelete ? '代理已删除' : '代理状态已更新');
        } catch (error) { toast(error.message, true); button.disabled = false; }
    });

    $('proxyTestAll').addEventListener('click', async () => {
        if (testingAll) return;
        testingAll = true; $('proxyTestAll').querySelector('span').textContent = '正在检测…';
        const queue = state.proxies.filter(proxy => proxy.enabled).map(proxy => proxy.id);
        await Promise.all(Array.from({length: Math.min(3, queue.length)}, async () => { while (queue.length) await testProxy(queue.shift()); }));
        testingAll = false; $('proxyTestAll').querySelector('span').textContent = '检测全部'; renderNodes(); toast('代理检测已完成，请查看各节点结果');
    });

    $('proxyDefaultSelect').addEventListener('change', event => {
        defaultSelection = event.target.value === routeValue(state.default) ? null : event.target.value;
    });
    $('proxyDefaultForm').addEventListener('submit', async event => {
        event.preventDefault();
        if (savingDefault) return;
        const route = routePayload($('proxyDefaultSelect').value);
        savingDefault = true; renderDefault();
        try { await api('/default', {method: 'PUT', body: JSON.stringify(route)}); state.default = route; defaultSelection = null; renderAccounts(); toast('默认登录出口已保存'); }
        catch (error) { toast(error.message, true); }
        finally { savingDefault = false; renderDefault(); }
    });

    $('proxyAccountsBody').addEventListener('change', event => {
        if (!event.target.matches('select')) return;
        const row = event.target.closest('tr');
        const account = state.accounts.find(item => item.email === row.dataset.email);
        const dirty = event.target.value !== routeValue(account);
        if (dirty) pending.set(account.email, event.target.value); else pending.delete(account.email);
        row.dataset.dirty = String(dirty); row.querySelector('button').disabled = !dirty;
    });

    $('proxyAccountsBody').addEventListener('click', async event => {
        const button = event.target.closest('button[data-action="save-route"]');
        if (!button) return;
        const row = button.closest('tr');
        const account = state.accounts.find(item => item.email === row.dataset.email);
        if (savingAccounts.has(account.email)) return;
        const route = routePayload(row.querySelector('select').value);
        savingAccounts.add(account.email);
        button.disabled = true; row.querySelector('select').disabled = true;
        try {
            await api('/accounts', {method: 'PUT', body: JSON.stringify({...route, email: account.email})});
            Object.assign(account, route); pending.delete(account.email);
            state.proxies.forEach(proxy => { proxy.account_count = state.accounts.filter(item => item.mode === 'proxy' && item.proxy_id === proxy.id).length; });
            metrics(); renderAccounts(); renderNodes(); toast('账号登录出口已保存');
        } catch (error) { toast(error.message, true); }
        finally { savingAccounts.delete(account.email); renderAccounts(); }
    });

    const requestedAccount = new URLSearchParams(window.location.search).get('account');
    if (requestedAccount || window.location.hash === '#accounts') {
        switchTab('accounts');
        if (requestedAccount) $('proxySearch').value = requestedAccount;
    }
    load();
})();
