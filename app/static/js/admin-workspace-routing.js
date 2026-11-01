const ADMIN_PAGES = new Set([
    '/admin', '/admin/account-pool', '/admin/phone-verification', '/admin/codes',
    '/admin/official-keys', '/admin/settings', '/admin/announcement', '/admin/renewal-requests',
]);

export function pageUrl(value) {
    const url = new URL(value, window.location.href);
    const path = url.pathname.replace(/\/$/, '');
    if (url.origin !== window.location.origin || !ADMIN_PAGES.has(path)) return null;
    // Match FastAPI's dashboard route directly; proxy redirects may use HTTP internally.
    if (path === '/admin') url.pathname = '/admin/';
    return url;
}

export function isPlainClick(event) {
    return !event.defaultPrevented && event.button === 0
        && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey;
}

export function linkDestination(event) {
    if (!isPlainClick(event)) return null;
    const link = event.target.closest('a[href]');
    if (!link || link.hasAttribute('download') || (link.target && link.target !== '_self')) return null;
    if (link.getAttribute('href').startsWith('#')) return null;
    return pageUrl(link.href);
}

export function syncAppearance(event) {
    if (event.key === 'ui_theme') {
        window.applySystemTheme(event.newValue);
        window.updateThemeToggleButton(window.getCurrentSystemTheme());
    }
    if (event.key === 'ui_style') window.applyUiStyle(event.newValue);
}
