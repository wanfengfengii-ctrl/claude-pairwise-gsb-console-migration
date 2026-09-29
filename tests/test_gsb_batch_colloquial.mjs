import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const app = fs.readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
const code = app.slice(app.indexOf('function conversationalizeGsb(id)'), app.indexOf('async function applyRecheck'));

function fixture() {
  const source = {
    verdict: 'Same',
    aReason: 'A 在 merge.py 修好保存问题，接口测试通过，不过浏览器流程没有执行。',
    bReason: 'B 在 rebase.ts 完成页面流程，Docker 验收通过，但新增测试还无法确认。',
  };
  const preview = {
    ...source,
    aReason: 'A 在 merge.py 把保存问题修好了，接口也测过，不过浏览器流程还没跑。',
  };
  const calls = [];
  const notices = [];
  const dialogs = [];
  const elements = {
    '#dialog': {open: false, close() { this.open = false; }},
    '#batch-colloquial-apply': {disabled: false},
  };
  const state = {
    page: 'reviews', reviewSelection: new Set(['pair-one', 'pair-two']),
    reviewItems: [{pair_id: 'pair-one', title: 'One'}, {pair_id: 'pair-two', title: 'Two'}],
    conversationalRunning: new Set(), colloquialBatch: null,
  };
  const api = async (path, options = {}) => {
    calls.push([path, options]);
    if (path === '/api/gsb-reviews/colloquialize') return {count: 2, jobs: [
      {pairId: 'pair-one', operationId: 'op-one', source},
      {pairId: 'pair-two', operationId: 'op-two', source},
    ]};
    if (path === '/api/operations/op-one' || path === '/api/operations/op-two') {
      return {status: 'completed', result: preview};
    }
    if (path === '/api/gsb-reviews/colloquialize/apply') return {
      applied: 2, skipped: 0, failed: 0,
      results: [{pair_id: 'pair-one', outcome: 'applied'}, {pair_id: 'pair-two', outcome: 'applied'}],
    };
    throw new Error('unexpected API ' + path);
  };
  const context = vm.createContext({
    state, api, $: (key) => elements[key], esc: (value) => String(value),
    pairIdentity: (id) => `<i>${id}</i>`,
    notify: (...args) => notices.push(args),
    showDialog: (html) => { dialogs.push(html); elements['#dialog'].open = true; },
    renderReviews: async () => {},
    window: {setTimeout: (callback) => callback()},
    Set, JSON,
  });
  vm.runInContext(code, context);
  return {context, state, calls, notices, dialogs, elements};
}

test('batch colloquialization previews all results without applying them', async () => {
  const {context, state, calls, dialogs} = fixture();
  await context.batchColloquializeSelected();
  assert.equal(state.colloquialBatch.items.length, 2);
  assert.equal(state.colloquialBatch.failures.length, 0);
  assert.match(dialogs.at(-1), /批量应用 2 条/);
  assert.equal(calls.filter(([path]) => path.endsWith('/apply')).length, 0);
});

test('batch apply sends previews and clears only applied selections', async () => {
  const {context, state, calls, elements} = fixture();
  await context.batchColloquializeSelected();
  await context.applyBatchColloquial();
  const applyCall = calls.find(([path]) => path.endsWith('/apply'));
  assert.ok(applyCall);
  assert.equal(JSON.parse(applyCall[1].body).items.length, 2);
  assert.equal(state.reviewSelection.size, 0);
  assert.equal(state.colloquialBatch, null);
  assert.equal(elements['#dialog'].open, false);
});
