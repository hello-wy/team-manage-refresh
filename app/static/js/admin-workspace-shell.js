import {linkDestination, pageUrl} from './admin-workspace-routing.js?v=20261108-proxy';

function syncNavigation(url) {
    const path = url.pathname.replace(/\/$/, '');
    const active = path === '/admin/announcement' ? '/admin/settings' : path;
    document.querySelectorAll('.desktop-nav-link, .sidebar-menu a[href]').forEach(link => {
        const selected = link.getAttribute('href') === active;
        const node = link.closest('.menu-item') || link;
        node.classList.toggle('active', selected);
        if (selected) link.setAttribute('aria-current', 'page');
        else link.removeAttribute('aria-current');
    });
}

function closeMenus() {
    document.getElementById('adminSidebar').classList.remove('open');
    document.getElementById('sidebarOverlay').classList.remove('show');
    document.getElementById('navbarAvatarMenu').hidden = true;
    document.getElementById('navbarAvatarBtn').setAttribute('aria-expanded', 'false');
}

export function createWorkspace(frame) {
    const setLoading = loading => {
        document.getElementById('adminWorkspace').setAttribute('aria-busy', String(loading));
    };
    const syncUrl = (value, {push = false} = {}) => {
        const url = pageUrl(value);
        if (!url) return;
        if (url.href !== window.location.href) {
            window.history[push ? 'pushState' : 'replaceState'](null, '', url);
        }
        syncNavigation(url);
    };
    const navigate = (value, {push = true} = {}) => {
        const url = pageUrl(value);
        if (!url) throw new Error(`未知的后台页面：${value}`);
        syncUrl(url, {push});
        closeMenus();
        setLoading(true);
        frame.contentWindow.location.replace(url.href);
    };
    return Object.freeze({navigate, syncUrl, setLoading});
}

export function connectShell(frame, workspace) {
    window.toggleSidebar = () => {
        document.getElementById('adminSidebar').classList.toggle('open');
        document.getElementById('sidebarOverlay').classList.toggle('show');
    };
    document.addEventListener('click', event => {
        const url = linkDestination(event);
        if (!url) return;
        event.preventDefault();
        workspace.navigate(url);
    });
    window.addEventListener('popstate', () => workspace.navigate(window.location.href, {push: false}));
    const loaded = () => {
        const url = new URL(frame.contentWindow.location.href);
        if (url.protocol === 'about:') return;
        if (url.pathname === '/login') {
            window.location.replace(url.href);
            return;
        }
        frame.contentDocument.dispatchEvent(new frame.contentWindow.Event('admin-workspace-ready'));
        document.title = frame.contentDocument.title || 'GPT Team 管理系统';
        workspace.syncUrl(url);
        workspace.setLoading(false);
    };
    frame.addEventListener('load', loaded);
    if (frame.contentDocument?.readyState === 'complete') loaded();
    workspace.syncUrl(window.location.href);
}
