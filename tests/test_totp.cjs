const assert = require('node:assert/strict');
const { webcrypto } = require('node:crypto');
const test = require('node:test');

const totp = require('../app/static/js/totp.js');

const RFC_SECRET = 'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ';

test('generates the RFC 6238 SHA-1 code for a known timestamp', async () => {
    const code = await totp.generate(RFC_SECRET, {
        crypto: webcrypto,
        digits: 8,
        timestamp: 59000,
    });
    assert.equal(code, '94287082');
});

test('uses the runtime Web Crypto provider by default', async () => {
    const original = globalThis.crypto;
    globalThis.crypto = webcrypto;
    try {
        const code = await totp.generate(RFC_SECRET, {digits: 8, timestamp: 59000});
        assert.equal(code, '94287082');
    } finally {
        globalThis.crypto = original;
    }
});

test('generates the same code when Web Crypto is unavailable', async () => {
    const code = await totp.generate(RFC_SECRET, {
        crypto: null,
        digits: 8,
        timestamp: 59000,
    });
    assert.equal(code, '94287082');
});

test('surfaces Web Crypto failures when the provider is available', async () => {
    const cryptoProvider = {
        subtle: {
            importKey: async () => {
                throw new Error('native crypto failure');
            },
        },
    };
    await assert.rejects(
        totp.generate(RFC_SECRET, {crypto: cryptoProvider, timestamp: 59000}),
        /native crypto failure/
    );
});

test('normalizes spaced secrets and otpauth URLs', () => {
    assert.equal(totp.normalizeSecret('jbsw y3dp-ehpk3pxp'), 'JBSWY3DPEHPK3PXP');
    assert.equal(
        totp.normalizeSecret('otpauth://totp/Test?secret=JBSWY3DPEHPK3PXP'),
        'JBSWY3DPEHPK3PXP'
    );
});

test('reports the remaining seconds and progress for the current window', () => {
    const state = totp.getWindow(12500);
    assert.equal(state.step, 0);
    assert.equal(state.remainingSeconds, 18);
    assert.equal(state.progress, 17.5 / 30);
});

test('rejects invalid Base32 secrets explicitly', async () => {
    await assert.rejects(
        totp.generate('INVALID*SECRET', {crypto: webcrypto, timestamp: 0}),
        /Base32/
    );
});
