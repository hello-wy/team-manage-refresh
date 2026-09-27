const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');

function runtime(search = '?page=3&per_page=20&search=alex&team_filter=2&seat_filter=premium&sort_by=email') {
    const location = {origin: 'http://localhost', search};
    const elements = new Map();
    const requests = [];
    let replaced = 0, cleared = 0;
    const region = {dataset: {currentPage: '1', totalPages: '4'},
        classList: {add() {}, remove() {}}, replaceWith() {replaced++;}};
    elements.set('accountPoolTableRegion', region);
    const context = vm.createContext({URL, URLSearchParams, AbortController, console,
        document: {addEventListener() {}, getElementById: id => elements.get(id), querySelector: () => null},
        window: {location, clearAccountPoolSelection() {cleared++;}, history: {
            replaceState(_state, _title, url) {location.search = url.search;},
        }},
        DOMParser: class {parseFromString() {return {getElementById: id => id === 'accountPoolTableRegion' ? region : null,
                                                   querySelector: () => null};}},
        fetch: (url, options) => new Promise(resolve => requests.push({url, options, resolve})),
        showToast: message => {throw new Error(message);},
    });
    vm.runInContext(fs.readFileSync(`${__dirname}/../app/static/js/account-pool.js`, 'utf8'), context);
    for (const name of ['initAccountPoolRowActions', 'initAccountPoolExportJobs', 'initAccountPoolPageSizeControl',
                        'initAccountPoolColumnToggler', 'formatQuotaResetTimes']) context[name] = () => {};
    return {context, elements, requests, replaced: () => replaced, cleared: () => cleared, location};
}

test('page size and ordering preserve team, seat and email filters', () => {
    const {context} = runtime();
    const params = context.getAccountPoolTableUrl({page: 1, perPage: 50, sortBy: 'deadline'}).searchParams;
    assert.equal(params.get('team_filter'), '2');
    assert.equal(params.get('seat_filter'), 'premium');
    assert.equal(params.get('search'), 'alex');
    assert.equal(params.get('per_page'), '50');
    assert.equal(params.get('page'), '1');
    assert.equal(params.get('sort_by'), 'deadline');
    const reset = context.getAccountPoolTableUrl({page: 1, teamFilter: '', seatFilter: '', search: ''}).searchParams;
    assert.equal(reset.get('team_filter'), '');
    assert.equal(reset.get('seat_filter'), '');
    assert.equal(reset.get('search'), '');
});

test('rapid filter changes combine intent and discard the older response', async () => {
    const env = runtime('');
    const first = env.context.refreshAccountPoolTable({page: 1, teamFilter: '1'});
    const second = env.context.refreshAccountPoolTable({page: 1, seatFilter: 'standard'});
    assert.equal(env.requests[1].url.searchParams.get('team_filter'), '1');
    assert.equal(env.requests[1].url.searchParams.get('seat_filter'), 'standard');
    assert.equal(env.requests[0].options.signal.aborted, true);
    env.requests[1].resolve({ok: true, text: async () => 'new'});
    await second;
    env.requests[0].resolve({ok: true, text: async () => 'old'});
    await first;
    assert.equal(env.replaced(), 1);
    assert.equal(env.cleared(), 1);
    assert.match(env.location.search, /team_filter=1/);
    assert.match(env.location.search, /seat_filter=standard/);
});

test('sorting refresh preserves selection while changing search clears it', async () => {
    const env = runtime();
    const sort = env.context.refreshAccountPoolTable({page: 1, sortBy: 'newest'});
    env.requests[0].resolve({ok: true, text: async () => 'html'});
    await sort;
    assert.equal(env.cleared(), 0);
    const search = env.context.refreshAccountPoolTable({page: 1, search: 'morgan'});
    env.requests[1].resolve({ok: true, text: async () => 'html'});
    await search;
    assert.equal(env.cleared(), 1);
});
