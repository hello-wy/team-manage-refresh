(function (root, factory) {
    const api = factory(root);
    if (typeof module === 'object' && module.exports) module.exports = api;
    if (root) root.totp = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, environment => {
    const BASE32_ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
    const DEFAULT_DIGITS = 6;
    const DEFAULT_PERIOD_SECONDS = 30;
    const HMAC_ALGORITHM = 'HMAC';
    const HASH_ALGORITHM = 'SHA-1';
    const COUNTER_BYTES = 8;

    function extractSecret(value) {
        const raw = String(value || '').trim();
        if (!raw.toLowerCase().startsWith('otpauth://')) return raw;
        return new URL(raw).searchParams.get('secret') || '';
    }

    function normalizeSecret(value) {
        return extractSecret(value)
            .toUpperCase()
            .replace(/[\s-]/g, '')
            .replace(/=+$/g, '');
    }

    function decodeBase32(value) {
        const normalized = normalizeSecret(value);
        if (!normalized) throw new Error('2FA 密钥为空');
        let bits = '';
        for (const character of normalized) {
            const index = BASE32_ALPHABET.indexOf(character);
            if (index < 0) throw new Error('2FA 密钥不是有效的 Base32 格式');
            bits += index.toString(2).padStart(5, '0');
        }
        const bytes = [];
        for (let offset = 0; offset + 8 <= bits.length; offset += 8) {
            bytes.push(Number.parseInt(bits.slice(offset, offset + 8), 2));
        }
        return new Uint8Array(bytes);
    }

    function buildCounter(timestamp, periodSeconds) {
        let counter = BigInt(Math.floor(timestamp / 1000 / periodSeconds));
        const bytes = new Uint8Array(COUNTER_BYTES);
        for (let index = COUNTER_BYTES - 1; index >= 0; index -= 1) {
            bytes[index] = Number(counter & 0xffn);
            counter >>= 8n;
        }
        return bytes;
    }

    function truncateDigest(digest, digits) {
        const bytes = new Uint8Array(digest);
        const offset = bytes[bytes.length - 1] & 0x0f;
        const value = (
            ((bytes[offset] & 0x7f) << 24)
            | ((bytes[offset + 1] & 0xff) << 16)
            | ((bytes[offset + 2] & 0xff) << 8)
            | (bytes[offset + 3] & 0xff)
        );
        return String(value % (10 ** digits)).padStart(digits, '0');
    }

    function getWindow(timestamp = Date.now(), periodSeconds = DEFAULT_PERIOD_SECONDS) {
        const periodMs = periodSeconds * 1000;
        const elapsedMs = ((timestamp % periodMs) + periodMs) % periodMs;
        const remainingMs = periodMs - elapsedMs;
        return Object.freeze({
            step: Math.floor(timestamp / periodMs),
            remainingMs,
            remainingSeconds: Math.ceil(remainingMs / 1000),
            progress: remainingMs / periodMs,
        });
    }

    async function generate(secret, options = {}) {
        const timestamp = options.timestamp ?? Date.now();
        const periodSeconds = options.periodSeconds ?? DEFAULT_PERIOD_SECONDS;
        const digits = options.digits ?? DEFAULT_DIGITS;
        const cryptoProvider = options.crypto ?? environment.crypto;
        if (!cryptoProvider?.subtle) throw new Error('当前浏览器不支持 Web Crypto');
        const key = await cryptoProvider.subtle.importKey(
            'raw', decodeBase32(secret),
            {name: HMAC_ALGORITHM, hash: HASH_ALGORITHM}, false, ['sign']
        );
        const digest = await cryptoProvider.subtle.sign(
            HMAC_ALGORITHM, key, buildCounter(timestamp, periodSeconds)
        );
        return truncateDigest(digest, digits);
    }

    return Object.freeze({
        DEFAULT_PERIOD_SECONDS,
        decodeBase32,
        generate,
        getWindow,
        normalizeSecret,
    });
});
