let recordsListRequest = null;

async function refreshRecordsTable(target = location.href, pushHistory = false) {
    recordsListRequest?.abort();
    const controller = new AbortController();
    recordsListRequest = controller;
    const url = new URL(target, location.origin);
    document.querySelector('.records-workspace-page .workbench-table-card')?.classList.add('is-loading');
    try {
        const response = await fetch(url, {
            credentials: 'same-origin',
            headers: {'X-Requested-With': 'XMLHttpRequest'},
            signal: controller.signal,
        });
        if (!response.ok) throw new Error('导出记录加载失败');
        const nextDocument = new DOMParser().parseFromString(await response.text(), 'text/html');
        if (controller.signal.aborted) return;
        for (const selector of [
            '.records-workspace-page .search-form-grid',
            '.records-workspace-page .workbench-table-card',
        ]) {
            const current = document.querySelector(selector);
            const next = nextDocument.querySelector(selector);
            if (!current || !next) throw new Error('导出记录响应缺少 ' + selector);
            current.replaceWith(next);
        }
        if (pushHistory) history.pushState({}, '', url);
        initColumns();
        formatQuotaResetTimes();
        if (window.lucide) lucide.createIcons();
    } catch (error) {
        if (error.name !== 'AbortError') {
            console.error(error);
            showToast(error.message, 'error');
        }
    } finally {
        if (recordsListRequest === controller) {
            document.querySelector('.records-workspace-page .workbench-table-card')?.classList.remove('is-loading');
        }
    }
}

document.addEventListener('click', event => {
    const link = event.target.closest('.records-workspace-page .pagination a, .records-workspace-page .search-form-grid a');
    if (!link || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    void refreshRecordsTable(link.href, true);
});

document.addEventListener('submit', event => {
    if (!event.target.matches('.records-workspace-page .search-form-grid')) return;
    event.preventDefault();
    const url = new URL(event.target.action);
    url.search = new URLSearchParams(new FormData(event.target)).toString();
    void refreshRecordsTable(url, true);
});

window.addEventListener('popstate', () => {
    if (location.pathname === '/admin/records') void refreshRecordsTable();
});

window.refreshRecordsTable = refreshRecordsTable;
