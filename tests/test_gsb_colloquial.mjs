import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const app = fs.readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
const code = app.slice(app.indexOf('function gsbEditorText()'), app.indexOf('async function confirmGsb(id)'));
const original = {verdict:'Same', aReason:'A 在 merge.py 修好了保存问题，接口已测过，但浏览器测试还没执行。', bReason:'B 在 merge.py 修好了保存问题，容器验收通过，不过新增测试是否运行还无法确认。'};
const rewritten = {...original, aReason:'A 在 merge.py 多加了一道保存检查，接口测过了，不过浏览器测试还没跑。'};
function fixture(api) {
  const elements = {
    '#dialog':{open:true}, '#gsb-colloquial':{}, '#gsb-colloquial-start':{disabled:false}, '#gsb-colloquial-preview':{},
    '#gsb-a-reason':{value:original.aReason}, '#gsb-b-reason':{value:original.bReason},
    'input[name="verdict"]:checked':{value:original.verdict},
  };
  const notices=[];
  const context=vm.createContext({$:key=>elements[key], api:api || (async path=>path.includes('/operations/') ? {status:'completed',result:rewritten}:{operationId:'rewrite-1'}), esc:value=>value, notify:(...args)=>notices.push(args), window:{setTimeout}});
  vm.runInContext(code,context);
  return {context,elements,notices};
}

test('preview leaves originals alone; apply and undo are local and preserve verdict', async()=>{
  const {context:c,elements:e}=fixture();
  await c.colloquializeGsb('pair-one');
  assert.equal(e['#gsb-a-reason'].value,original.aReason);
  c.applyGsbColloquial();
  assert.equal(e['#gsb-a-reason'].value,rewritten.aReason);
  assert.equal(e['input[name="verdict"]:checked'].value,'Same');
  c.undoGsbColloquial();
  assert.equal(e['#gsb-a-reason'].value,original.aReason);
});
test('edits or changed verdict prevent stale preview application', async()=>{
  for(const selector of ['#gsb-a-reason','input[name="verdict"]:checked']) {
    const {context:c,elements:e,notices}=fixture(); await c.colloquializeGsb('pair-one');
    e[selector].value='Changed'; c.applyGsbColloquial();
    assert.equal(e[selector].value,'Changed'); assert.match(notices.at(-1)[0],/重新口语化/);
  }
});
test('late model result cannot touch a different dialog', async()=>{
  let resolveOperation, entered;
  const started=new Promise(resolve=>entered=resolve);
  const {context:c,elements:e}=fixture(async path=>{
    if(!path.includes('/operations/')) return {operationId:'rewrite-1'};
    entered(); return await new Promise(resolve=>resolveOperation=resolve);
  });
  const pending=c.colloquializeGsb('pair-one'); await started;
  const nextPreview={textContent:'new record'};
  e['#gsb-colloquial']={}; e['#gsb-colloquial-preview']=nextPreview;
  resolveOperation({status:'completed',result:rewritten}); await pending;
  assert.equal(nextPreview.textContent,'new record'); assert.equal(nextPreview.innerHTML,undefined);
});
test('failure retains text and restores retry button', async()=>{
  const {context:c,elements:e}=fixture(async()=>{throw new Error('模型暂不可用');});
  await c.colloquializeGsb('pair-one');
  assert.equal(e['#gsb-a-reason'].value,original.aReason);
  assert.equal(e['#gsb-colloquial-start'].disabled,false);
  assert.match(e['#gsb-colloquial-preview'].textContent,/模型暂不可用/);
});
test('undo refuses to overwrite later manual edits', async()=>{
  const {context:c,elements:e,notices}=fixture(); await c.colloquializeGsb('pair-one'); c.applyGsbColloquial();
  e['#gsb-b-reason'].value='Later manual edit'; c.undoGsbColloquial();
  assert.equal(e['#gsb-b-reason'].value,'Later manual edit'); assert.match(notices.at(-1)[0],/手动调整/);
});
