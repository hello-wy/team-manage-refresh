function closeEditor(dialog) {
    dialog.close();
    dialog.remove();
}

async function persistExpiration(dialog, options) {
    const buttons = dialog.querySelectorAll('button');
    buttons.forEach(button => { button.disabled = true; });
    const errorText = dialog.querySelector('[data-error]');
    errorText.hidden = true;
    try {
        await options.request(`/admin/official-keys/${options.entry.id}/expiration`, {
            method: 'PUT', body: JSON.stringify({expires_at: options.value})
        });
        closeEditor(dialog);
        showToast('到期时间已保存', 'success');
        await options.reload();
    } catch (error) {
        errorText.textContent = error.message;
        errorText.hidden = false;
    } finally {
        buttons.forEach(button => { button.disabled = false; });
    }
}

function editExpiration(entry, options) {
    const dialog = document.createElement('dialog');
    dialog.className = 'page-confirm official-expiration-dialog';
    dialog.setAttribute('aria-labelledby', 'officialExpirationTitle');
    dialog.append(document.getElementById('officialExpirationTemplate').content.cloneNode(true));
    dialog.querySelector('[data-account]').textContent = entry.account;
    const input = dialog.querySelector('input');
    input.value = options.view.input_value;
    dialog.querySelector('form').addEventListener('submit', event => {
        event.preventDefault();
        persistExpiration(dialog, {...options, entry, value: input.value || null});
    });
    dialog.querySelector('[data-clear]').addEventListener('click', () => {
        persistExpiration(dialog, {...options, entry, value: null});
    });
    dialog.querySelector('[data-cancel]').addEventListener('click', () => closeEditor(dialog));
    dialog.addEventListener('close', () => dialog.remove());
    dialog.addEventListener('keydown', event => event.stopPropagation());
    document.body.append(dialog);
    dialog.showModal();
    input.focus();
}

export function expirationCell(entry, options) {
    const cell = document.createElement('td');
    // Only the shared server-rendered datetime component supplies this HTML.
    cell.innerHTML = options.view.html;
    const source = document.createElement('small');
    source.className = 'official-expiration-source';
    source.textContent = options.view.source === 'team' ? '工作台同步'
        : options.view.source === 'manual' ? '手动设置' : options.view.note;
    source.title = options.view.note;
    cell.append(source);
    if (options.view.can_edit) {
        cell.append(options.action('设置时间', () => editExpiration(entry, options)));
    }
    return cell;
}
