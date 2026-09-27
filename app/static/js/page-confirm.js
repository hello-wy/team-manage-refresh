// A page dialog stays usable when the browser suppresses native confirm().
let pageConfirmationOpen = false;

function confirmPageAction(message) {
    if (pageConfirmationOpen) return Promise.resolve(false);
    pageConfirmationOpen = true;
    const trigger = document.activeElement;
    const dialog = document.createElement('dialog');
    dialog.className = 'page-confirm';
    dialog.setAttribute('aria-labelledby', 'pageConfirmTitle');
    dialog.setAttribute('aria-describedby', 'pageConfirmMessage');
    dialog.innerHTML = '<h3 id="pageConfirmTitle">确认操作</h3>'
        + '<p id="pageConfirmMessage"></p>'
        + '<div class="page-confirm-actions">'
        + '<button type="button" class="btn btn-secondary" data-cancel autofocus>取消</button>'
        + '<button type="button" class="btn btn-primary" data-confirm>确认继续</button></div>';
    dialog.querySelector('p').textContent = message;
    document.body.appendChild(dialog);
    // Keep the underlying member dialog's keyboard handlers out of this dialog.
    dialog.addEventListener('keydown', event => event.stopPropagation());
    return new Promise(resolve => {
        let settled = false;
        const finish = confirmed => {
            if (settled) return;
            settled = true;
            dialog.close();
            dialog.remove();
            pageConfirmationOpen = false;
            if (trigger?.isConnected) trigger.focus();
            resolve(confirmed);
        };
        dialog.querySelector('[data-cancel]').addEventListener('click', () => finish(false));
        dialog.querySelector('[data-confirm]').addEventListener('click', () => finish(true));
        dialog.addEventListener('cancel', event => {
            event.preventDefault();
            event.stopPropagation();
            finish(false);
        });
        dialog.addEventListener('close', () => finish(false));
        dialog.showModal();
    });
}
