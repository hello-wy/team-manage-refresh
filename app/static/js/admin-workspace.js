import {syncAppearance} from './admin-workspace-routing.js?v=20261108-proxy';
import {connectShell, createWorkspace} from './admin-workspace-shell.js?v=20261108-proxy';
import {connectContent} from './admin-workspace-content.js?v=20261108-proxy';

window.addEventListener('storage', syncAppearance);

const frame = document.getElementById('adminWorkspaceFrame');
if (frame) {
    window.adminWorkspaceController = createWorkspace(frame);
    connectShell(frame, window.adminWorkspaceController);
} else if (window.parent !== window) {
    let connected = false;
    const connect = () => {
        if (connected || !window.parent.adminWorkspaceController) return;
        connected = true;
        connectContent(window.parent.adminWorkspaceController);
    };
    document.addEventListener('DOMContentLoaded', connect);
    document.addEventListener('admin-workspace-ready', connect);
}
