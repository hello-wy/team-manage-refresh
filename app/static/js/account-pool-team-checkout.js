async function readTeamCheckoutResponse(response) {
    const text = await response.text();
    let result;
    try {
        result = JSON.parse(text);
    } catch (error) {
        throw new Error(`生成支付链接失败：服务器返回非 JSON 响应（HTTP ${response.status}）`);
    }
    if (!result || typeof result !== 'object') {
        throw new Error(`生成支付链接失败：服务器响应结构无效（HTTP ${response.status}）`);
    }
    if (!response.ok || !result.success) {
        throw new Error(result.error || `生成支付链接失败（HTTP ${response.status}）`);
    }
    let url;
    try {
        url = new URL(result.url);
    } catch (error) {
        throw new Error('支付服务未返回有效的支付链接');
    }
    if (url.protocol !== 'https:' || url.hostname !== 'checkout.stripe.com' || url.username || url.password) {
        throw new Error('支付服务返回的 Stripe 支付链接无效');
    }
    return url.href;
}

function showTeamCheckoutLink(dialog, url) {
    const result = dialog.querySelector('[data-result]');
    result.querySelector('textarea').value = url;
    result.querySelector('a').href = url;
    result.hidden = false;
}

async function copyTeamCheckoutLink(dialog) {
    const feedback = dialog.querySelector('[data-feedback]');
    try {
        await navigator.clipboard.writeText(dialog.querySelector('[data-result] textarea').value);
        feedback.textContent = '支付链接已复制';
    } catch (error) {
        feedback.textContent = `复制失败：${error.message}；可直接选中下方链接复制`;
    }
}

async function submitAccountPoolTeamCheckout({button, dialog, form, account}) {
    const feedback = dialog.querySelector('[data-feedback]');
    const fieldset = dialog.querySelector('fieldset');
    dialog.querySelector('[data-result]').hidden = true;
    feedback.textContent = '正在重新登录账号并生成支付链接，请稍候…';
    fieldset.disabled = true;
    button.disabled = true;
    try {
        const response = await fetch(`/admin/account-pool/${account.entryId}/team-checkout`, {
            method: 'POST', credentials: 'same-origin', cache: 'no-store',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({workspace_id: account.workspaceId || '', coupon: form.elements.coupon.value.trim()}),
        });
        showTeamCheckoutLink(dialog, await readTeamCheckoutResponse(response));
        feedback.textContent = '支付链接已生成，请在下方查看或复制。';
    } catch (error) {
        feedback.textContent = error.message;
    } finally {
        fieldset.disabled = false;
        button.disabled = false;
    }
}

function teamCheckoutDialogContent() {
    return '<form><fieldset style="border:0;padding:0;margin:0">'
        + '<h3 id="teamCheckoutTitle">充值 Team</h3><p data-account></p>'
        + '<p>请使用个人免费账号；套餐、席位和地区沿用支付后台配置。生成链接后将在下方展示。</p>'
        + '<label for="teamCheckoutCoupon">优惠码或分享链接</label>'
        + '<input id="teamCheckoutCoupon" class="form-control" name="coupon" autocomplete="off" '
        + 'placeholder="留空使用支付后台已保存的优惠码">'
        + '<p data-feedback role="status" aria-live="polite"></p><div class="page-confirm-actions">'
        + '<button type="button" class="btn btn-secondary" data-cancel>关闭</button>'
        + '<button type="submit" class="btn btn-primary">生成支付链接</button>'
        + '</div></fieldset></form><section data-result hidden style="margin-top:20px">'
        + '<label for="teamCheckoutUrl">支付链接</label>'
        + '<textarea id="teamCheckoutUrl" class="form-control" rows="4" readonly '
        + 'style="word-break:break-all" aria-label="支付链接"></textarea>'
        + '<div class="page-confirm-actions"><button type="button" class="btn btn-secondary" data-copy>复制链接</button>'
        + '<a class="btn btn-primary" referrerpolicy="no-referrer">前往支付</a></div></section>';
}

function openAccountPoolTeamCheckout(button) {
    const dialog = document.createElement('dialog');
    dialog.className = 'page-confirm';
    dialog.setAttribute('aria-labelledby', 'teamCheckoutTitle');
    dialog.innerHTML = teamCheckoutDialogContent();
    dialog.querySelector('[data-account]').textContent = button.dataset.email;
    const form = dialog.querySelector('form');
    form.addEventListener('submit', event => {
        event.preventDefault();
        if (dialog.querySelector('fieldset').disabled) return;
        void submitAccountPoolTeamCheckout({button, dialog, form, account: {...button.dataset}});
    });
    dialog.querySelector('[data-copy]').addEventListener('click', () => void copyTeamCheckoutLink(dialog));
    dialog.querySelector('[data-cancel]').addEventListener('click', () => dialog.close());
    dialog.addEventListener('keydown', event => event.stopPropagation());
    dialog.addEventListener('cancel', event => {
        event.stopPropagation();
        if (dialog.querySelector('fieldset').disabled) event.preventDefault();
    });
    dialog.addEventListener('close', () => dialog.remove(), {once: true});
    document.body.appendChild(dialog);
    dialog.showModal();
    dialog.querySelector('input').focus();
}

document.addEventListener('click', event => {
    const button = event.target.closest('.account-pool-team-checkout-button');
    if (button && !button.disabled) openAccountPoolTeamCheckout(button);
});
