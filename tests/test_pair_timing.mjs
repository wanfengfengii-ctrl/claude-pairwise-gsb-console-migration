import assert from "node:assert/strict";
import fs from "node:fs";

const app = fs.readFileSync(new URL("../web/app.js", import.meta.url), "utf8");
const api = fs.readFileSync(new URL("../pairwise_console/api.py", import.meta.url), "utf8");

assert.match(app, /A 侧开发时间/);
assert.match(app, /B 侧开发时间/);
assert.match(app, /function updateArmTimings/);
assert.match(app, /预计剩余/);
assert.match(app, /window\.setInterval\(\(\) => \{[^}]*updateArmTimings\(\)/s);
assert.match(api, /aa\.prompt_sent_at a_prompt_sent_at/);
assert.match(api, /bb\.prompt_sent_at b_prompt_sent_at/);
assert.match(api, /t\.estimated_minutes_min,t\.estimated_minutes_max/);

console.log("pair timing checks passed");
