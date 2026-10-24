const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');

const context = { TextDecoder };
vm.runInNewContext(fs.readFileSync('app/static/js/phone-verification-stream.js', 'utf8'), context);
const { consume } = context.PhoneVerificationStream;
const encoder = new TextEncoder();

function response(chunks) {
  return new Response(new ReadableStream({
    start(controller) {
      chunks.forEach((chunk) => controller.enqueue(chunk));
      controller.close();
    },
  }), { headers: { 'Content-Type': 'application/x-ndjson' } });
}

test('decodes Chinese progress across arbitrary network and UTF-8 boundaries', async () => {
  const bytes = encoder.encode('{"type":"progress","stage":"wait_code","message":"等待验证码"}\n{"type":"result","phone":"+233****789"}\n');
  const events = [];
  await consume(response(Array.from(bytes, (byte) => new Uint8Array([byte]))), (event) => events.push(event));
  assert.equal(events[0].message, '等待验证码');
  assert.equal(events[1].type, 'result');
});

test('disconnect before final result stays unconfirmed', async () => {
  await assert.rejects(consume(response([encoder.encode('{"type":"progress","stage":"wait_code"}\n')]), () => {}), /操作结果尚未确认/);
});

test('failure event reaches caller without becoming success', async () => {
  const events = [];
  await consume(response([encoder.encode('{"type":"error","error":"上游请求失败（HTTP 404）"}')]), (event) => events.push(event));
  assert.equal(events.length, 1);
  assert.equal(events[0].type, 'error');
  assert.match(events[0].error, /404/);
});

test('rejects login redirect instead of inventing progress', async () => {
  await assert.rejects(consume(new Response('<html>登录</html>'), () => {}), /未返回流程进度/);
});

test('malformed or unknown progress is surfaced', async () => {
  await assert.rejects(consume(response([encoder.encode('{"type":"unknown"}\n')]), () => {}), /无效的流程进度/);
  await assert.rejects(consume(response([encoder.encode('{broken}\n')]), () => {}), /JSON|property|Unexpected/);
});
