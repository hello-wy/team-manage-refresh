const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');

const source = readFileSync('app/static/js/main.js', 'utf8');

test('code generation UI does not request or render generation-time seat capacity', () => {
    assert.doesNotMatch(source, /generation-capacity/);
    assert.doesNotMatch(source, /refreshCodeGenerationCapacityPreview/);
    assert.doesNotMatch(source, /renderGeneratedCodeCapacitySnapshot/);
});
