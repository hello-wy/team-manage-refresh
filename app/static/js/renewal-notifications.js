const RENEWAL_API_URL = '/admin/renewal-requests/api?status_filter=pending';
const RENEWAL_BADGE_URL = '/admin/renewal-requests/pending-count';
// 多标签页同步：每 30s 轻量轮询 pending_count。Page 不可见时跳过，
// 避免后台 tab 持续抢请求。
const RENEWAL_BADGE_POLL_MS = 30000;

function describeRenewalHttpError(status) {
    if (status === 401 || status === 403) return '登录已失效，请刷新页面后重试';
    if (status === 429) return '操作过于频繁，请稍后再试';
    if (status >= 500) return '服务异常，请稍后重试';
    if (status === 404) return '记录不存在或已被处理';
    return null;
}

function escapeHtml(text) {
    if (text === null || text === undefined) return '';
    return String(text)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function formatRenewalDateTime(value) {
    if (!value) return '-';
    try {
        const d = new Date(value);
        if (Number.isNaN(d.getTime())) return value;
        const pad = (n) => String(n).padStart(2, '0');
        return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
    } catch (e) {
        return value;
    }
}

function syncRenewalBadge(count) {
    const badge = document.getElementById('navbarTaskBadge');
    if (badge) {
        badge.textContent = count > 0 ? String(count > 99 ? '99+' : count) : '0';
        if (count > 0) {
            badge.hidden = false;
            badge.style.display = '';
        } else {
            badge.hidden = true;
        }
    }
    const sidebarCount = document.getElementById('sidebarTaskCount');
    if (sidebarCount) {
        sidebarCount.textContent = count > 0 ? ` (${count})` : '';
        sidebarCount.style.display = count > 0 ? '' : 'none';
    }
}

function renderRenewalRequestsList(items) {
    const container = document.getElementById('renewalRequestsModalContent');
    if (!container) return;

    if (!items || items.length === 0) {
        container.innerHTML = `
            <div class="renewal-modal-empty">
                <i data-lucide="check-circle-2" style="width: 36px; height: 36px;"></i>
                <p>当前没有待处理的续期申请</p>
            </div>
        `;
        if (window.lucide) lucide.createIcons();
        return;
    }

    const rows = items.map(renderRenewalCard).join('');

    container.innerHTML = `<div class="renewal-task-list">${rows}</div>`;
    if (window.lucide) lucide.createIcons();
}

async function refreshRenewalRequestsModal() {
    const container = document.getElementById('renewalRequestsModalContent');
    if (!container) return;

    try {
        const response = await fetch(RENEWAL_API_URL, {
            method: 'GET',
            headers: { 'Accept': 'application/json' },
        });
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }
        const data = await response.json();
        if (!data.success) {
            throw new Error(data.error || '加载失败');
        }
        const items = data.requests || [];
        renderRenewalRequestsList(items);
        syncRenewalBadge(data.pending_count !== undefined ? data.pending_count : items.length);
    } catch (err) {
        console.error('加载续期任务失败', err);
        container.innerHTML = `
            <div class="renewal-modal-empty">
                <i data-lucide="alert-triangle" style="width: 36px; height: 36px;"></i>
                <p>加载失败，请稍后重试</p>
            </div>
        `;
        if (window.lucide) lucide.createIcons();
        if (typeof showToast === 'function') {
            showToast(err.message || '加载续期任务失败', 'error');
        }
    }
}

async function syncRenewalBadgeFromServer() {
    if (typeof document !== 'undefined' && document.hidden) return;
    try {
        const response = await fetch(RENEWAL_BADGE_URL, {
            method: 'GET',
            headers: { 'Accept': 'application/json' },
        });
        if (!response.ok) return;
        const data = await response.json();
        if (data && data.success && typeof data.pending_count === 'number') {
            syncRenewalBadge(data.pending_count);
        }
    } catch (_) {
        /* 静默失败：badge 不阻塞主流程 */
    }
}

if (typeof window !== 'undefined') {
    // 启动时若顶栏 badge 存在则定期与服务端对齐，多标签页处理后自然收敛。
    window.addEventListener('DOMContentLoaded', () => {
        if (document.getElementById('navbarTaskBadge')) {
            setInterval(syncRenewalBadgeFromServer, RENEWAL_BADGE_POLL_MS);
            document.addEventListener('visibilitychange', () => {
                if (!document.hidden) syncRenewalBadgeFromServer();
            });
        }
    });
}

function openRenewalRequestsModal() {
    if (typeof showModal !== 'function') {
        window.location.href = '/admin/renewal-requests';
        return;
    }
    showModal('renewalRequestsModal');
    refreshRenewalRequestsModal();
}
