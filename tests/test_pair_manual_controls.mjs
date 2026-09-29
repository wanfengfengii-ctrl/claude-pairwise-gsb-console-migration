import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

const app = fs.readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');

test('task detail uses an exact task endpoint and renders the full prompt', () => {
  assert.match(app, /\/api\/tasks\/\$\{encodeURIComponent\(id\)\}/);
  assert.match(app, /function taskPromptHtml\(task\)/);
  assert.match(app, /复制题目/);
});

test('pair detail exposes bounded manual controls for each requested operation', () => {
  assert.match(app, /\/reset-retries/);
  assert.match(app, /Pair 开发失败/);
  assert.match(app, /重试次数已归零，旧代码、Session、轨迹和提交均已保留/);
  assert.match(app, /\/arms\/\$\{arm\}\/queue/);
  assert.match(app, /\/difficulty/);
  assert.match(app, /重置 A 并重新排队|`重置 \$\{row\.arm\} 并重新排队`/);
  assert.match(app, /const peerActive = data\.arms\.some/);
  assert.match(app, /自动复评证据已保留/);
});

test('platform-bound pairs visibly disable local mutation controls', () => {
  assert.match(app, /pairIsPlatformLocked/);
  assert.match(app, /已提交或绑定 SOLO-QA，不能再重排或编辑难度/);
});

test('replaced pair exposes only its failed arm when the peer passed Docker validation', () => {
  const source = app.split('function pairRetryControls(data) {')[1]
    .split('async function showPair(')[0];
  const render = new Function('pairIsPlatformLocked', 'esc',
    `return function pairRetryControls(data) {${source}`)(() => false, String);
  const html = render({
    id: 'pair-example', status: 'failed', stage: 'replaced',
    development_failure_count: 2, arms: [
      { arm: 'A', status: 'completed', commit_sha: 'a'.repeat(40) },
      { arm: 'B', status: 'failed', commit_sha: '' },
    ], checks: [{ arm: 'A', status: 'passed', commit_sha: 'a'.repeat(40) }],
  });
  assert.match(html, /queuePairArm\('pair-example','A',this\)" disabled/);
  assert.match(html, /queuePairArm\('pair-example','B',this\)"(?! disabled)/);
  assert.match(html, /resetPairRetries\('pair-example',this\)" disabled/);
});

test('deferred new pair cannot be manually restarted while yielding priority', () => {
  const source = app.split('function pairRetryControls(data) {')[1]
    .split('async function showPair(')[0];
  const render = new Function('pairIsPlatformLocked', 'esc',
    `return function pairRetryControls(data) {${source}`)(() => false, String);
  const html = render({
    id: 'pair-deferred', status: 'deferred_priority', stage: 'ready_to_start',
    arms: [{ arm: 'A', status: 'queued' }, { arm: 'B', status: 'queued' }], checks: [],
  });
  assert.match(html, /queuePairArm\('pair-deferred','A',this\)" disabled/);
  assert.match(html, /queuePairArm\('pair-deferred','B',this\)" disabled/);
  assert.match(html, /resetPairRetries\('pair-deferred',this\)" disabled/);
});

test('paused pair permits its stopped side to be requeued', () => {
  const source = app.split('function pairRetryControls(data) {')[1]
    .split('async function showPair(')[0];
  const render = new Function('pairIsPlatformLocked', 'esc',
    `return function pairRetryControls(data) {${source}`)(() => false, String);
  const html = render({
    id: 'pair-paused', status: 'paused', stage: 'manual_pause',
    arms: [{ arm: 'A', status: 'completed', commit_sha: 'a'.repeat(40) },
      { arm: 'B', status: 'infrastructure_paused' }], checks: [],
  });
  assert.match(html, /queuePairArm\('pair-paused','B',this\)"(?! disabled)/);
});

test('backend projects retain normal recording, rerecording and playback controls', () => {
  assert.doesNotMatch(app, /纯后端无需录像/);
  assert.match(app, /人工重新录制/);
  assert.match(app, /人工录制/);
  assert.match(app, /重新自动录制/);
  assert.match(app, /playRecording/);
});
