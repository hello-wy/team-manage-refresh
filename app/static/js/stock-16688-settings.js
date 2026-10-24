document.addEventListener('DOMContentLoaded', () => {
    const form = document.getElementById('stock16688Form');
    if (!form) return;
    form.addEventListener('submit', async event => {
        event.preventDefault();
        const button = form.querySelector('button[type="submit"]');
        if (button.disabled) return;
        button.disabled = true;
        const input = id => document.getElementById(id);
        const secret = input('stock16688Secret');
        try {
            const response = await fetch('/admin/settings/16688', {
                method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({gateway: input('stock16688Gateway').value.trim(),
                    app_id: input('stock16688AppId').value.trim(), goods_no: input('stock16688GoodsNo').value.trim(),
                    secret: secret.value}),
            });
            const result = await response.json();
            if (!response.ok || !result.success) throw new Error(result.error || '保存失败');
            input('stock16688Gateway').value = result.gateway;
            secret.value = '';
            secret.placeholder = '已配置，留空保持不变';
            showToast(result.message, 'success');
        } catch (error) {
            showToast(error.message || '保存失败', 'error');
        } finally {
            button.disabled = false;
        }
    });
});
