const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');

async function load(payload, current = '63') {
  const select = { value: current, children: [{ value: current }],
    replaceChildren(...children) { this.children = children; this.value = children[0].value; } };
  const classes = new Set();
  const status = { textContent: '', hidden: false, classList: {
    add(name) { classes.add(name); },
    toggle(name, enabled) { if (enabled) classes.add(name); else classes.delete(name); },
  } };
  const context = {
    document: {
      getElementById: (id) => id === 'phoneCountrySelect' ? select : status,
      createElement: () => ({}),
    },
    fetch: async (url) => {
      assert.equal(url, '/admin/phone-verification/countries');
      return { ok: payload.success, json: async () => payload };
    },
  };
  await vm.runInNewContext(fs.readFileSync('app/static/js/phone-verification-countries.js', 'utf8'), context);
  return { select, status, classes };
}

test('shows country names and IDs without replacing the saved selection', async () => {
  const { select, status } = await load({ success: true, countries: [
    { id: '0', name: '俄罗斯' }, { id: '63', name: '捷克共和国' },
  ] });
  assert.equal(select.value, '63');
  assert.deepEqual(select.children.map((option) => option.textContent), ['俄罗斯（0）', '捷克共和国（63）']);
  assert.equal(status.hidden, true);
});

test('missing saved country remains explicit and other countries stay selectable', async () => {
  const { select, status } = await load({ success: true, countries: [{ id: '0', name: '俄罗斯' }] });
  assert.equal(select.value, '63');
  assert.equal(select.children.length, 2);
  assert.match(select.children[0].textContent, /平台未返回/);
  assert.equal(status.hidden, false);
});

test('upstream error is visible and preserves the existing setting', async () => {
  const { select, status, classes } = await load({ success: false, error: '读取国家列表失败' });
  assert.equal(select.value, '63');
  assert.equal(status.textContent, '读取国家列表失败');
  assert.ok(classes.has('is-error'));
});

test('rejects malformed country values instead of submitting them', async () => {
  const { select, status } = await load({ success: true, countries: [{ id: '../bad', name: '无效' }] });
  assert.equal(select.value, '63');
  assert.match(status.textContent, /无效/);
});
