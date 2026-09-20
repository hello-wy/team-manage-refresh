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
    const BITS_PER_BYTE = 8;
    const WORD_BYTES = 4;
    const SHA1_BLOCK_BYTES = 64;
    const SHA1_LENGTH_BYTES = 8;
    const SHA1_SCHEDULE_WORDS = 80;
    const SHA1_INITIAL_WORDS = Object.freeze([
        0x67452301, 0xefcdab89, 0x98badcfe, 0x10325476, 0xc3d2e1f0,
    ]);
    const SHA1_ROUND_CONSTANTS = Object.freeze([
        0x5a827999, 0x6ed9eba1, 0x8f1bbcdc, 0xca62c1d6,
    ]);
    const HMAC_INNER_PAD = 0x36;
    const HMAC_OUTER_PAD = 0x5c;
    const UINT32_RANGE = 0x100000000;

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

    function rotateLeft(value, amount) {
        return ((value << amount) | (value >>> (32 - amount))) >>> 0;
    }

    function readUint32(bytes, offset) {
        return (
            (bytes[offset] << 24)
            | (bytes[offset + 1] << 16)
            | (bytes[offset + 2] << 8)
            | bytes[offset + 3]
        ) >>> 0;
    }

    function writeUint32(bytes, offset, value) {
        bytes[offset] = value >>> 24;
        bytes[offset + 1] = value >>> 16;
        bytes[offset + 2] = value >>> 8;
        bytes[offset + 3] = value;
    }

    function padSha1Message(message) {
        const contentBytes = message.length + 1 + SHA1_LENGTH_BYTES;
        const paddedLength = Math.ceil(contentBytes / SHA1_BLOCK_BYTES) * SHA1_BLOCK_BYTES;
        const padded = new Uint8Array(paddedLength);
        const bitLength = message.length * BITS_PER_BYTE;
        padded.set(message);
        padded[message.length] = 0x80;
        writeUint32(padded, paddedLength - SHA1_LENGTH_BYTES, Math.floor(bitLength / UINT32_RANGE));
        writeUint32(padded, paddedLength - WORD_BYTES, bitLength >>> 0);
        return padded;
    }

    function buildSha1Schedule(message, offset) {
        const schedule = new Uint32Array(SHA1_SCHEDULE_WORDS);
        const blockWords = SHA1_BLOCK_BYTES / WORD_BYTES;
        for (let index = 0; index < blockWords; index += 1) {
            schedule[index] = readUint32(message, offset + (index * WORD_BYTES));
        }
        for (let index = blockWords; index < SHA1_SCHEDULE_WORDS; index += 1) {
            schedule[index] = rotateLeft(
                schedule[index - 3] ^ schedule[index - 8]
                ^ schedule[index - 14] ^ schedule[index - 16],
                1
            );
        }
        return schedule;
    }

    function getSha1RoundValue(index, second, third, fourth) {
        if (index < 20) return (second & third) | (~second & fourth);
        if (index < 40) return second ^ third ^ fourth;
        if (index < 60) return (second & third) | (second & fourth) | (third & fourth);
        return second ^ third ^ fourth;
    }

    function compressSha1(state, schedule) {
        let [first, second, third, fourth, fifth] = state;
        for (let index = 0; index < SHA1_SCHEDULE_WORDS; index += 1) {
            const constant = SHA1_ROUND_CONSTANTS[Math.floor(index / 20)];
            const next = (
                rotateLeft(first, 5) + getSha1RoundValue(index, second, third, fourth)
                + fifth + constant + schedule[index]
            ) >>> 0;
            fifth = fourth;
            fourth = third;
            third = rotateLeft(second, 30);
            second = first;
            first = next;
        }
        return new Uint32Array([
            (state[0] + first) >>> 0,
            (state[1] + second) >>> 0,
            (state[2] + third) >>> 0,
            (state[3] + fourth) >>> 0,
            (state[4] + fifth) >>> 0,
        ]);
    }

    function sha1(message) {
        const padded = padSha1Message(message);
        let state = new Uint32Array(SHA1_INITIAL_WORDS);
        for (let offset = 0; offset < padded.length; offset += SHA1_BLOCK_BYTES) {
            state = compressSha1(state, buildSha1Schedule(padded, offset));
        }
        const digest = new Uint8Array(state.length * WORD_BYTES);
        state.forEach((value, index) => writeUint32(digest, index * WORD_BYTES, value));
        return digest;
    }

    function concatenateBytes(...arrays) {
        const result = new Uint8Array(arrays.reduce((length, bytes) => length + bytes.length, 0));
        let offset = 0;
        arrays.forEach(bytes => {
            result.set(bytes, offset);
            offset += bytes.length;
        });
        return result;
    }

    function buildHmacPad(key, padValue) {
        const normalizedKey = key.length > SHA1_BLOCK_BYTES ? sha1(key) : key;
        const pad = new Uint8Array(SHA1_BLOCK_BYTES);
        pad.fill(padValue);
        normalizedKey.forEach((value, index) => {
            pad[index] ^= value;
        });
        return pad;
    }

    function hmacSha1(key, message) {
        const inner = buildHmacPad(key, HMAC_INNER_PAD);
        const outer = buildHmacPad(key, HMAC_OUTER_PAD);
        return sha1(concatenateBytes(outer, sha1(concatenateBytes(inner, message))));
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
        const secretBytes = decodeBase32(secret);
        const counter = buildCounter(timestamp, periodSeconds);
        const cryptoProvider = options.crypto === undefined ? environment.crypto : options.crypto;
        let digest;
        if (cryptoProvider?.subtle) {
            const key = await cryptoProvider.subtle.importKey(
                'raw', secretBytes,
                {name: HMAC_ALGORITHM, hash: HASH_ALGORITHM}, false, ['sign']
            );
            digest = await cryptoProvider.subtle.sign(HMAC_ALGORITHM, key, counter);
        } else {
            digest = hmacSha1(secretBytes, counter);
        }
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
