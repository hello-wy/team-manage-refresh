// A page dialog stays usable when the browser suppresses native confirm().
let accountPoolConfirmationOpen = false;

function confirmAccountPoolAction(message) {
    if (accountPoolConfirmationOpen) return Promise.resolve(false);
    accountPoolConfirmationOpen = true;
    const trigger = document.activeElement;
    const dialog = document.createElement('dialog');
    dialog.className = 'account-pool-confirm';
    dialog.setAttribute('aria-labelledby', 'accountPoolConfirmTitle');
    dialog.setAttribute('aria-describedby', 'accountPoolConfirmMessage');
    dialog.innerHTML = '<h3 id="accountPoolConfirmTitle">确认操作</h3>'
        + '<p id="accountPoolConfirmMessage"></p>'
        + '<div class="account-pool-confirm-actions">'
        + '<button type="button" class="btn btn-secondary" data-cancel autofocus>取消</button>'
        + '<button type="button" class="btn btn-primary" data-confirm>确认继续</button></div>';
    dialog.querySelector('p').textContent = message;
    document.body.appendChild(dialog);
    return new Promise(resolve => {
        let settled = false;
        const finish = confirmed => {
            if (settled) return;
            settled = true;
            dialog.close();
            dialog.remove();
            accountPoolConfirmationOpen = false;
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
