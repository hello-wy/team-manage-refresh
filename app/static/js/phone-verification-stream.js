(function (root) {
  async function consume(response, onEvent) {
    if (!response.ok) throw new Error(`请求失败（HTTP ${response.status}）`);
    if (!response.headers.get('content-type')?.includes('application/x-ndjson')) {
      throw new Error('服务端未返回流程进度，请刷新页面后重新检查');
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let pending = '';
    let terminal = false;
    function accept(line) {
      const event = JSON.parse(line);
      if (!['progress', 'result', 'error'].includes(event.type) || terminal) {
        throw new Error('服务端返回了无效的流程进度');
      }
      terminal = event.type !== 'progress';
      onEvent(event);
    }
    try {
      while (true) {
        const { value, done } = await reader.read();
        pending += decoder.decode(value, { stream: !done });
        const lines = pending.split('\n');
        pending = lines.pop();
        lines.filter((line) => line.trim()).forEach(accept);
        if (done) break;
      }
      if (pending.trim()) accept(pending);
      if (!terminal) throw new Error('进度连接已中断，操作结果尚未确认，请检查账号状态和服务端日志');
    } finally {
      reader.releaseLock();
    }
  }
  root.PhoneVerificationStream = { consume };
})(globalThis);
