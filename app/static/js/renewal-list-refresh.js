let renewalListRequest = null;

async function refreshRenewalRequests(target = location.href, pushHistory = false) {
    renewalListRequest?.abort();
    const controller = new AbortController();
    renewalListRequest = controller;
    const url = new URL(target, location.origin);
    try {
        const response = await fetch(url, {
            credentials: 'same-origin',
            headers: {'X-Requested-With': 'XMLHttpRequest'},
            signal: controller.signal,
        });
        if (!response.ok) throw new Error('待处理任务加载失败');
        const nextDocument = new DOMParser().parseFromString(await response.text(), 'text/html');
        if (controller.signal.aborted) return;
        for (const selector of [
            '.records-workspace-page .page-header-meta',
            '.records-workspace-page .stats-container',
            '.records-workspace-page .search-form-grid',
            '.records-workspace-page .workbench-table-card',
        ]) {
            const current = document.querySelector(selector);
            const next = nextDocument.querySelector(selector);
            if (!current || !next) throw new Error('待处理任务响应缺少 ' + selector);
            current.replaceWith(next);
        }
        const pending = nextDocument.querySelector(
            '.records-workspace-page .stats-container .stat-card:nth-child(2) .stat-value'
        );
        if (pending) syncRenewalBadge(Number(pending.textContent));
        if (pushHistory) history.pushState({}, '', url);
        if (window.lucide) lucide.createIcons();
    } catch (error) {
        if (error.name !== 'AbortError') {
            console.error(error);
            showToast(error.message, 'error');
        }
    }
}

document.addEventListener('submit', event => {
    if (!event.target.matches('.records-workspace-page .search-form-grid')) return;
    event.preventDefault();
    const url = new URL(event.target.action);
    url.search = new URLSearchParams(new FormData(event.target)).toString();
    void refreshRenewalRequests(url, true);
});

document.addEventListener('click', event => {
    const link = event.target.closest('.records-workspace-page .search-form-grid a');
    if (!link || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    void refreshRenewalRequests(link.href, true);
});

window.addEventListener('popstate', () => {
    if (location.pathname === '/admin/renewal-requests') void refreshRenewalRequests();
});

window.refreshRenewalRequests = refreshRenewalRequests;
