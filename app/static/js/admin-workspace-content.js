import {linkDestination, pageUrl} from './admin-workspace-routing.js';

function bridgeHistory(workspace) {
    const replaceState = window.history.replaceState.bind(window.history);
    // The shell owns browser history so Back does not create duplicate frame entries.
    for (const method of ['pushState', 'replaceState']) {
        window.history[method] = (state, title, url) => {
            replaceState(state, title, url);
            workspace.syncUrl(window.location.href, {push: method === 'pushState'});
        };
    }
    window.addEventListener('hashchange', () => workspace.syncUrl(window.location.href));
}

function bridgeLinks(workspace) {
    document.addEventListener('click', event => {
        const url = linkDestination(event);
        if (!url) return;
        event.preventDefault();
        workspace.navigate(url);
    });
}

function bridgeForms(workspace) {
    document.addEventListener('submit', event => {
        const form = event.target;
        if (event.defaultPrevented || form.method.toLowerCase() !== 'get') return;
        if (form.target && form.target !== '_self') return;
        const url = pageUrl(form.action);
        if (!url) return;
        event.preventDefault();
        url.search = new URLSearchParams(new FormData(form, event.submitter)).toString();
        workspace.navigate(url);
    });
}

export function connectContent(workspace) {
    bridgeHistory(workspace);
    bridgeLinks(workspace);
    bridgeForms(workspace);
    workspace.syncUrl(window.location.href);
}
