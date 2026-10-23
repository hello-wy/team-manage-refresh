(function () {
  const settingsForm = document.getElementById('phoneSettingsForm');
  const settingsStatus = document.getElementById('phoneSettingsStatus');
  const settingsButton = document.getElementById('phoneSettingsSave');
  const providerState = document.getElementById('phoneProviderState');
  const accountSelect = document.getElementById('phoneAccountSelect');
  const startButton = document.getElementById('phoneStartButton');
  const resultPanel = document.getElementById('phoneResult');

  function showStatus(element, message, kind) {
    element.textContent = message;
    element.classList.toggle('is-error', kind === 'error');
    element.classList.toggle('is-success', kind === 'success');
  }

  async function readResponse(response) {
    const payload = await response.json();
    if (!response.ok || !payload.success) throw new Error(payload.error || `请求失败（HTTP ${response.status}）`);
    return payload;
  }

  settingsForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    settingsButton.disabled = true;
    showStatus(settingsStatus, '正在保存设置…', '');
    try {
      const values = Object.fromEntries(new FormData(settingsForm));
      await readResponse(await fetch('/admin/phone-verification/settings', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(values),
      }));
      settingsForm.elements.api_key.value = '';
      providerState.classList.add('is-ready');
      providerState.innerHTML = '<i data-lucide="check-circle-2"></i> HeroSMS 已配置';
      startButton.disabled = !accountSelect || !accountSelect.value;
      showStatus(settingsStatus, 'HeroSMS 设置已加密保存。', 'success');
      if (window.initLucideIcons) window.initLucideIcons();
    } catch (error) {
      showStatus(settingsStatus, error.message, 'error');
    } finally {
      settingsButton.disabled = false;
    }
  });

  startButton?.addEventListener('click', async () => {
    if (!accountSelect?.value) return;
    startButton.disabled = true;
    resultPanel.hidden = false;
    resultPanel.className = 'phone-result is-busy';
    resultPanel.textContent = '正在登录账号并检查手机号验证步骤；如需接码，将等待 HeroSMS 返回验证码。';
    try {
      const response = await fetch(`/admin/phone-verification/${encodeURIComponent(accountSelect.value)}`, { method: 'POST' });
      const payload = await readResponse(response);
      resultPanel.className = `phone-result is-${payload.warning ? 'error' : 'success'}`;
      resultPanel.textContent = payload.warning || `手机号验证成功：${payload.phone}`;
      const option = accountSelect.selectedOptions[0];
      if (option && !option.textContent.includes(payload.phone)) option.textContent += ` · ${payload.phone}`;
    } catch (error) {
      resultPanel.className = 'phone-result is-error';
      resultPanel.textContent = error.message;
    } finally {
      startButton.disabled = false;
    }
  });
})();
