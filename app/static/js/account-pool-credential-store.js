window.accountPoolCredentialStore = (() => {
    const endpoint = '/admin/account-pool/credentials';
    const requests = new Map();

    async function fetchCredentials(email) {
        const response = await fetch(
            `${endpoint}?email=${encodeURIComponent(email)}`,
            {credentials: 'same-origin', cache: 'no-store'}
        );
        const payload = await response.json();
        if (!response.ok || !payload.success) {
            throw new Error(payload.error || '读取账号凭据失败');
        }
        return Object.freeze({...payload.data});
    }

    function load(email, options = {}) {
        const normalized = String(email || '').trim().toLowerCase();
        if (!normalized) return Promise.reject(new Error('账号邮箱为空'));
        if (options.fresh) requests.delete(normalized);
        if (requests.has(normalized)) return requests.get(normalized);
        const request = fetchCredentials(normalized).catch(error => {
            requests.delete(normalized);
            throw error;
        });
        requests.set(normalized, request);
        return request;
    }

    function clear(email) {
        requests.delete(String(email || '').trim().toLowerCase());
    }

    return Object.freeze({clear, load});
})();
