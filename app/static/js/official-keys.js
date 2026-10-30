import {expirationCell} from './official-key-expiration.js?v=20261029-expiration';

const KEY_PREVIEW_PREFIX_LENGTH = 10;
const KEY_PREVIEW_SUFFIX_LENGTH = 4;
const KEY_PREVIEW_LENGTH = KEY_PREVIEW_PREFIX_LENGTH + KEY_PREVIEW_SUFFIX_LENGTH;
const form = document.getElementById('officialKeyForm');
const account = document.getElementById('officialKeyAccount');
const keyValue = document.getElementById('officialKeyValue');
const cancel = document.getElementById('officialKeyCancel');
const save = document.getElementById('officialKeySave');
const status = document.getElementById('officialKeyStatus');
const rows = document.getElementById('officialKeyRows');
const title = document.getElementById('officialKeyFormTitle');
const endpoint = '/admin/official-keys';
const exportAll = document.getElementById('officialKeyExportAll');
const exportProgress = document.getElementById('officialKeyExportStatus');
const exportErrors = document.getElementById('officialKeyExportErrors');

async function request(url, options = {}) {
    const response = await fetch(url, {
        ...options, cache: 'no-store', headers: {'Content-Type': 'application/json'}
    });
    const contentType = response.headers.get('content-type') || '';
    if (!contentType.includes('application/json')) {
        throw new Error(`服务返回非 JSON 响应（HTTP ${response.status}），请检查服务日志`);
    }
    const data = await response.json();
    if (!response.ok) {
        const detail = typeof data.detail === 'string' ? data.detail : '请求失败，请检查输入';
        throw new Error(detail);
    }
    return data;
}

function resetForm() {
    form.reset();
    delete form.dataset.entryId;
    cancel.hidden = true;
    title.textContent = '新增官key';
}

function edit(entry) {
    form.dataset.entryId = String(entry.id);
    account.value = entry.account;
    keyValue.value = entry.api_key;
    cancel.hidden = false;
    title.textContent = '编辑官key';
    account.focus();
    form.scrollIntoView({behavior: 'smooth', block: 'center'});
}

function action(label, handler) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn btn-sm btn-secondary';
    button.textContent = label;
    button.addEventListener('click', async () => {
        button.disabled = true;
        try {
            await handler();
        } catch (error) {
            showToast(error.message, 'error');
        } finally {
            button.disabled = false;
        }
    });
    return button;
}

async function remove(entry) {
    if (!await confirmPageAction(`确定删除账号「${entry.account}」的官key？`)) return;
    await request(`${endpoint}/${entry.id}`, {method: 'DELETE'});
    if (form.dataset.entryId === String(entry.id)) resetForm();
    showToast('已删除', 'success');
    await load();
}

function exportLabel(result) {
    if (!result) return '未导出';
    const labels = {
        exported: '已导出', changed: '有修改，待同步', exporting: '导出中',
        failed: '导出失败', uncertain: '结果不确定，请核对'
    };
    const suffix = result.account_id ? `（#${result.account_id}）` : '';
    return `${labels[result.status]}${suffix}${result.error ? `：${result.error}` : ''}`;
}

function balanceLabel(result) {
    if (!result) return '未查询';
    if (result.status === 'error') return `查询失败：${result.error}`;
    const balance = result.balance;
    if (!balance) return '余额响应无效';
    return `可用 ${balance.total_available} ${balance.currency}`;
}

function balanceCell(result) {
    const cell = document.createElement('td');
    const value = document.createElement('span');
    value.className = 'official-key-balance-value';
    value.textContent = balanceLabel(result);
    cell.append(value);
    return cell;
}

async function push(entry) {
    try {
        const data = await request(`${endpoint}/${entry.id}/sub2api`, {method: 'POST'});
        showToast(`已导出到 Sub2API（#${data.export.account_id}）`, 'success');
    } finally {
        await load();
    }
}

function row(entry, {exported, expiration, balance}) {
    const tr = document.createElement('tr');
    tr.dataset.entryId = String(entry.id);
    tr.dataset.account = entry.account;
    const accountCell = document.createElement('td');
    accountCell.textContent = entry.account;
    const keyCell = document.createElement('td');
    const code = document.createElement('code');
    code.textContent = entry.api_key.length > KEY_PREVIEW_LENGTH
        ? `${entry.api_key.slice(0, KEY_PREVIEW_PREFIX_LENGTH)}…${entry.api_key.slice(-KEY_PREVIEW_SUFFIX_LENGTH)}`
        : entry.api_key;
    keyCell.append(code);
    const exportCell = document.createElement('td');
    exportCell.textContent = exportLabel(exported);
    const actionsCell = document.createElement('td');
    const actions = document.createElement('div');
    actions.className = 'official-key-actions';
    actions.append(
        action('复制', () => copyToClipboard(entry.api_key)),
        action('导出到 Sub2API', () => push(entry)),
        action('编辑', () => edit(entry)),
        action('删除', () => remove(entry))
    );
    actionsCell.append(actions);
    const expiryCell = expirationCell(entry, {view: expiration, request, reload: load, action});
    tr.append(accountCell, expiryCell, keyCell, balanceCell(balance), exportCell, actionsCell);
    return tr;
}

async function load() {
    status.hidden = false;
    status.dataset.error = 'false';
    status.textContent = '正在加载…';
    rows.replaceChildren();
    document.getElementById('officialKeyCount').textContent = '';
    try {
        const data = await request(`${endpoint}/data`);
        rows.replaceChildren(...data.keys.map(entry => row(
            entry, {
                exported: data.exports[entry.id], expiration: data.expirations[entry.id],
                balance: data.balances[entry.id],
            }
        )));
        exportAll.disabled = data.keys.length === 0;
        document.getElementById('officialKeyCount').textContent = `(${data.keys.length})`;
        status.hidden = data.keys.length > 0;
        status.textContent = '暂无官key，请先添加账号和 API Key。';
    } catch (error) {
        exportAll.disabled = true;
        status.dataset.error = 'true';
        status.textContent = `加载失败：${error.message}`;
    }
}

function setExportBusy(busy) {
    document.querySelectorAll('#officialKeyRows button, #officialKeyForm button, '
        + '#officialKeyRefresh, #officialKeyExportAll').forEach(button => {
        button.disabled = busy;
    });
}

function appendExportError(entry, error) {
    const item = document.createElement('li');
    item.textContent = `${entry.account}：${error.message}`;
    exportErrors.append(item);
    exportErrors.hidden = false;
}

async function pushAll() {
    const entries = Array.from(rows.children, tr => ({
        id: tr.dataset.entryId, account: tr.dataset.account
    }));
    setExportBusy(true);
    exportErrors.replaceChildren();
    exportErrors.hidden = true;
    exportProgress.hidden = false;
    let succeeded = 0;
    try {
        for (const [index, entry] of entries.entries()) {
            exportProgress.textContent = `正在导出 ${index + 1}/${entries.length}：${entry.account}`;
            try {
                await request(`${endpoint}/${entry.id}/sub2api`, {method: 'POST'});
                succeeded += 1;
            } catch (error) {
                appendExportError(entry, error);
            }
        }
        exportProgress.textContent = `导出完成：成功 ${succeeded} 条，失败 ${entries.length - succeeded} 条。`;
    } finally {
        setExportBusy(false);
        await load();
    }
}

form.addEventListener('submit', async event => {
    event.preventDefault();
    save.disabled = true;
    cancel.disabled = true;
    const id = form.dataset.entryId;
    try {
        await request(id ? `${endpoint}/${id}` : endpoint, {
            method: id ? 'PUT' : 'POST',
            body: JSON.stringify({account: account.value, api_key: keyValue.value})
        });
        resetForm();
        showToast('官key 已保存', 'success');
        await load();
    } catch (error) {
        showToast(error.message, 'error');
    } finally {
        save.disabled = false;
        cancel.disabled = false;
    }
});
cancel.addEventListener('click', resetForm);
document.getElementById('officialKeyRefresh').addEventListener('click', load);
exportAll.addEventListener('click', pushAll);
load();
