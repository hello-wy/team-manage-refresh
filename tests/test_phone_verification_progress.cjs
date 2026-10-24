const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');

function element() {
  return {
    textContent: '', dataset: {}, children: [], attributes: {}, handlers: {}, disabled: false,
    classList: { toggle() {}, add() {} },
    addEventListener(name, handler) { this.handlers[name] = handler; },
    setAttribute(name, value) { this.attributes[name] = value; },
    removeAttribute(name) { delete this.attributes[name]; },
    replaceChildren() { this.children = []; },
    append(child) { this.children.push(child); },
    querySelector(selector) {
      return this.children.find((child) => selector.includes(`"${child.dataset.stage}"`));
    },
  };
}

function load(events) {
  const ids = ['phoneSettingsForm', 'phoneSettingsStatus', 'phoneSettingsSave', 'phoneProviderState',
    'phoneAccountSelect', 'phoneStartButton', 'phoneResult', 'phoneProgress', 'phoneProgressTitle',
    'phoneProgressElapsed', 'phoneProgressSteps', 'phoneProgressAttempt'];
  const elements = Object.fromEntries(ids.map((id) => [id, element()]));
  elements.phoneSettingsForm.elements = [];
  elements.phoneAccountSelect.value = '1';
  elements.phoneAccountSelect.selectedOptions = [{ textContent: 'test-account' }];
  const snapshots = [];
  const context = {
    document: { getElementById: (id) => elements[id], createElement: element },
    setInterval: () => 1, clearInterval() {},
    fetch: async () => ({}),
    PhoneVerificationStream: {
      async consume(response, notify) {
        for (const event of events) {
          notify(event);
          snapshots.push({
            stage: event.stage, title: elements.phoneProgressTitle.textContent,
            attempt: elements.phoneProgressAttempt.textContent,
            result: elements.phoneResult.textContent,
            steps: Object.fromEntries(elements.phoneProgressSteps.children.map((item) =>
              [item.dataset.stage, item.dataset.state])),
          });
        }
      },
    },
  };
  vm.runInNewContext(fs.readFileSync('app/static/js/phone-verification.js', 'utf8'), context);
  return { elements, snapshots, run: elements.phoneStartButton.handlers.click };
}

const progress = (...stages) => stages.map((stage) => ({ type: 'progress', stage }));
const success = { type: 'result', success: true, phone: '+420****123' };


test('timeout remains a warning and new rental resets steps while showing attempt count', async () => {
  const { elements, snapshots, run } = load([
    ...progress('login', 'rent_number', 'send_code', 'wait_code', 'cancel_number', 'rent_number',
      'send_code', 'wait_code', 'verify_code', 'finish_rental', 'oauth', 'save'), success,
  ]);
  await run();
  const cancelled = snapshots.find((item) => item.stage === 'cancel_number');
  assert.equal(cancelled.title, '取消超时号码');
  assert.equal(cancelled.steps.wait_code, 'warning');
  assert.equal(cancelled.steps.verify_code, 'pending');
  assert.match(cancelled.result, /确认取消成功后/);
  const retry = snapshots.filter((item) => item.stage === 'rent_number')[1];
  assert.match(retry.attempt, /第 2 次取号.*已取消 1 个/);
  assert.equal(retry.steps.login, 'done');
  assert.equal(retry.steps.rent_number, 'active');
  for (const stage of ['send_code', 'wait_code', 'verify_code', 'finish_rental', 'oauth', 'save']) {
    assert.equal(retry.steps[stage], 'pending');
  }
  assert.equal(elements.phoneProgressTitle.textContent, '手机号验证完成');
  assert.equal(elements.phoneResult.className, 'phone-result is-success');
  assert.equal(elements.phoneStartButton.disabled, false);
});

test('cancellation failure stays explicit without a new attempt or success', async () => {
  const { elements, run } = load([
    ...progress('login', 'rent_number', 'send_code', 'wait_code', 'cancel_number'),
    { type: 'error', error: 'HeroSMS 取消租号失败：未确认取消' },
  ]);
  await run();
  assert.equal(elements.phoneResult.className, 'phone-result is-error');
  assert.match(elements.phoneProgressTitle.textContent, /取消超时号码未完成/);
  assert.match(elements.phoneProgressAttempt.textContent, /第 1 次取号.*已取消 0 个/);
  assert.match(elements.phoneResult.textContent, /取消租号失败/);
});

test('attempt counter resets when a separate operation starts', async () => {
  const { elements, run } = load([
    ...progress('login', 'rent_number', 'send_code', 'wait_code', 'cancel_number', 'rent_number'),
    { type: 'error', error: '发送失败' },
  ]);
  await run();
  await run();
  assert.match(elements.phoneProgressAttempt.textContent, /第 2 次取号.*已取消 1 个/);
});
