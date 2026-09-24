let codesListRequest = null;

function replaceCodesRegion(nextDocument, selector) {
    const current = document.querySelector(selector);
    const next = nextDocument.querySelector(selector);
    if (!current || !next) throw new Error('兑换码列表响应缺少 ' + selector);
    current.replaceWith(next);
}

async function refreshCodesTable(target = window.location.href, pushHistory = false) {
    codesListRequest?.abort();
    const controller = new AbortController();
    codesListRequest = controller;
    const url = new URL(target, window.location.origin);
    const table = document.querySelector('.codes-workspace-page .workbench-table-card');
    table?.classList.add('is-loading');
    try {
        const response = await fetch(url, {
            credentials: 'same-origin',
            headers: {'X-Requested-With': 'XMLHttpRequest'},
            signal: controller.signal,
        });
        if (!response.ok) throw new Error('兑换码列表加载失败');
        const nextDocument = new DOMParser().parseFromString(await response.text(), 'text/html');
        if (controller.signal.aborted) return;
        const floatingBar = document.body.querySelector(':scope > #bulkActionBar');
        floatingBar?.remove();
        document.getElementById('bulk-action-home')?.remove();
        for (const selector of [
            '.codes-workspace-page .page-header-meta',
            '.codes-workspace-page .stats-container',
            '.codes-workspace-page .filter-tabs',
            '.codes-workspace-page .codes-search-form',
            '.codes-workspace-page .workbench-table-card',
        ]) replaceCodesRegion(nextDocument, selector);
        if (pushHistory) history.pushState({}, '', url);
        initColumnToggler('.codes-workspace-page .data-table', 'codes_list_columns');
        if (window.lucide) lucide.createIcons();
    } catch (error) {
        if (error.name !== 'AbortError') {
            console.error(error);
            showToast(error.message, 'error');
        }
    } finally {
        if (codesListRequest === controller) {
            document.querySelector('.codes-workspace-page .workbench-table-card')?.classList.remove('is-loading');
        }
    }
}

document.addEventListener('click', event => {
    const link = event.target.closest('.codes-workspace-page .workbench-table-card .pagination a, .codes-workspace-page .codes-search-form a');
    if (!link || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    void refreshCodesTable(link.href, true);
});

document.addEventListener('submit', event => {
    if (!event.target.matches('.codes-workspace-page .codes-search-form')) return;
    event.preventDefault();
    const url = new URL(event.target.action);
    url.search = new URLSearchParams(new FormData(event.target)).toString();
    void refreshCodesTable(url, true);
});

window.addEventListener('popstate', () => {
    if (location.pathname === '/admin/codes') void refreshCodesTable();
});

window.refreshCodesTable = refreshCodesTable;
