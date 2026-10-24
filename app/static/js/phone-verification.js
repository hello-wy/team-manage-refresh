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

  const progressPanel = document.getElementById('phoneProgress');
  const progressTitle = document.getElementById('phoneProgressTitle');
  const progressElapsed = document.getElementById('phoneProgressElapsed');
  const progressAttempt = document.getElementById('phoneProgressAttempt');
  const progressSteps = document.getElementById('phoneProgressSteps');
  const stages = [
    ['login', '登录账号', '正在登录账号并验证密码和 2FA…'],
    ['rent_number', '获取手机号', '正在向 HeroSMS 获取手机号…'],
    ['send_code', '发送验证码', '正在请求 OpenAI 发送短信验证码…'],
    ['wait_code', '等待验证码', '短信已请求发送，正在等待 HeroSMS 返回验证码…'],
    ['verify_code', '验证手机号', '已收到验证码，正在提交验证…'],
    ['finish_rental', '结束接码', '正在向 HeroSMS 确认接码完成…'],
    ['oauth', '完成账号授权', '正在完成工作区选择和 OAuth 授权…'],
    ['save', '保存结果', '正在加密保存手机号和账号授权结果…'],
  ];
  const TIMER_INTERVAL_MS = 1000;
  let currentStep = null;
  let stageStartedAt = 0;
  let numberAttempts = 0;

  function initializeProgress() {
    progressPanel.hidden = false;
    progressSteps.replaceChildren();
    currentStep = null;
    numberAttempts = 0;
    progressAttempt.textContent = '';
    stages.forEach(([key, label]) => {
      const item = document.createElement('li');
      item.dataset.stage = key;
      item.dataset.state = 'pending';
      item.textContent = label;
      progressSteps.append(item);
    });
    progressTitle.textContent = '正在连接…';
    progressElapsed.textContent = '';
  }

  function startNumberAttempt() {
    numberAttempts += 1;
    Array.from(progressSteps.children).forEach((item) => {
      if (item.dataset.stage !== 'login') item.dataset.state = 'pending';
      item.removeAttribute('aria-current');
    });
    currentStep = null;
    progressAttempt.textContent = `第 ${numberAttempts} 次取号 · 已取消 ${numberAttempts - 1} 个超时号码`;
  }

  function showNumberCancellation() {
    currentStep.dataset.state = 'warning';
    stageStartedAt = Date.now();
    progressTitle.textContent = '取消超时号码';
    resultPanel.textContent = '等待验证码超时，正在取消当前号码；确认取消成功后将重新获取号码并继续接码…';
    progressElapsed.textContent = '当前步骤已用时 0 秒';
  }

  function updateProgress(stage) {
    if (stage === 'cancel_number') {
      showNumberCancellation();
      return;
    }
    const definition = stages.find(([key]) => key === stage);
    if (!definition) throw new Error('服务端返回了未知的验证步骤');
    if (currentStep) {
      if (currentStep.dataset.state !== 'warning') currentStep.dataset.state = 'done';
      currentStep.removeAttribute('aria-current');
    }
    if (stage === 'rent_number') startNumberAttempt();
    currentStep = Array.from(progressSteps.children).find((item) => item.dataset.stage === stage);
    currentStep.dataset.state = 'active';
    currentStep.setAttribute('aria-current', 'step');
    stageStartedAt = Date.now();
    progressTitle.textContent = definition[1];
    resultPanel.textContent = definition[2];
    progressElapsed.textContent = '当前步骤已用时 0 秒';
  }

  function setBusy(busy) {
    startButton.disabled = busy;
    accountSelect.disabled = busy;
    Array.from(settingsForm.elements).forEach((element) => { element.disabled = busy; });
    startButton.textContent = busy ? '正在添加手机号…' : '添加手机号并接码';
    progressPanel.setAttribute('aria-busy', String(busy));
  }

  function showResult(payload, option) {
    if (currentStep) {
      currentStep.dataset.state = 'done';
      currentStep.removeAttribute('aria-current');
    }
    if (payload.warning) {
      progressSteps.querySelector('[data-stage="finish_rental"]').dataset.state = 'warning';
    }
    progressTitle.textContent = payload.warning ? '验证完成，有待处理事项' : '手机号验证完成';
    resultPanel.className = `phone-result is-${payload.warning ? 'warning' : 'success'}`;
    resultPanel.textContent = payload.warning
      ? `手机号验证成功：${payload.phone}。${payload.warning}` : `手机号验证成功：${payload.phone}`;
    if (option && !option.textContent.includes(payload.phone)) option.textContent += ` · ${payload.phone}`;
  }

  function showFailure(error) {
    if (currentStep) {
      currentStep.dataset.state = 'error';
      currentStep.removeAttribute('aria-current');
    }
    progressTitle.textContent = currentStep ? `${progressTitle.textContent}未完成` : '请求未完成';
    resultPanel.className = 'phone-result is-error';
    resultPanel.textContent = error.message;
  }

  async function startVerification() {
    if (!accountSelect?.value || startButton.disabled) return;
    const entryId = accountSelect.value;
    const option = accountSelect.selectedOptions[0];
    setBusy(true);
    initializeProgress();
    resultPanel.hidden = false;
    resultPanel.className = 'phone-result is-busy';
    resultPanel.textContent = '正在连接服务端，等待流程开始…';
    const timer = setInterval(() => {
      if (currentStep) progressElapsed.textContent = `当前步骤已用时 ${Math.floor((Date.now() - stageStartedAt) / TIMER_INTERVAL_MS)} 秒`;
    }, TIMER_INTERVAL_MS);
    try {
      const response = await fetch(`/admin/phone-verification/${encodeURIComponent(entryId)}`, { method: 'POST' });
      let result = null;
      await PhoneVerificationStream.consume(response, (event) => {
        if (event.type === 'error') throw new Error(event.error);
        if (event.type === 'progress') updateProgress(event.stage);
        if (event.type === 'result') result = event;
      });
      showResult(result, option);
    } catch (error) {
      showFailure(error);
    } finally {
      clearInterval(timer);
      setBusy(false);
    }
  }

  startButton?.addEventListener('click', startVerification);
})();
