(() => {
    'use strict';
    const root = document.getElementById('proxyPage');
    if (!root) return;
    const $ = id => document.getElementById(id);
    const state = {proxies: []};
    const testing = new Set();
    let editing = null;
    let testingAll = false;
    let savingEditor = false;
    const icons = () => window.lucide?.createIcons();
    const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char]));
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

    function metrics() {
        $('proxyTotal').textContent = state.proxies.length;
        $('proxyOnline').textContent = state.proxies.filter(proxy => proxy.enabled && proxy.test_status === 'online').length;
        $('proxyDisabled').textContent = state.proxies.filter(proxy => !proxy.enabled).length;
        $('proxyNodeCount').textContent = state.proxies.length;
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

    function render() { metrics(); renderNodes(); }

    async function load() {
        $('proxyPageMessage').hidden = false;
        $('proxyPageMessage').classList.remove('error');
        $('proxyPageMessage').textContent = '正在加载代理…';
        $('proxyRetry').hidden = true;
        try {
            const data = await api('/data');
            state.proxies = data.proxies;
            $('proxyPageMessage').hidden = true;
            render();
        } catch (error) {
            $('proxyPageMessage').textContent = error.message;
            $('proxyPageMessage').classList.add('error');
            $('proxyRetry').hidden = false;
        }
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
        finally { testing.delete(id); metrics(); renderNodes(); }
    }

    $('proxySearch').addEventListener('input', renderNodes);
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

    load();
})();
