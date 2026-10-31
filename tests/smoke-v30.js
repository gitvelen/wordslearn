/* v3.0 自选词库浏览器冒烟：setup→词库→新建词单（命中/候选/库外）→保存→（造 senses）→选用→建计划→today⊆库 */
const { chromium } = require('/usr/local/lib/node_modules/@playwright/cli/node_modules/playwright-core');
const BASE = 'http://localhost:8091';
let pass = 0, fail = 0;
const ok = (cond, name) => { if (cond) { pass++; console.log('  ✓', name); } else { fail++; console.log('  ✗ FAIL', name); } };

(async () => {
  // 预清理：保证幂等（删测试用户的库/计划/状态、上轮造的 flurbo 词与 senses）
  const { execSync } = require('child_process');
  execSync(`python3 -c "
import sqlite3
db = sqlite3.connect('/tmp/wl-smoke.db')
uid = db.execute(\\\"SELECT id FROM users WHERE username='e2e_lib2'\\\").fetchone()
if uid:
    uid = uid[0]
    libs = [r[0] for r in db.execute('SELECT id FROM word_libraries WHERE user_id=?', (uid,))]
    for lid in libs:
        db.execute('DELETE FROM library_words WHERE library_id=?', (lid,))
    db.execute('DELETE FROM word_libraries WHERE user_id=?', (uid,))
    for p in db.execute(\\\"SELECT id FROM study_plans WHERE user_id=? AND status='active'\\\", (uid,)).fetchall():
        db.execute('DELETE FROM plan_words WHERE plan_id=?', (p[0],))
    db.execute(\\\"DELETE FROM study_plans WHERE user_id=?\\\", (uid,))
    db.execute('DELETE FROM user_word_state WHERE user_id=?', (uid,))
    db.execute('DELETE FROM user_quiz_used WHERE user_id=?', (uid,))
    db.execute(\\\"DELETE FROM daily_logs WHERE user_id=?\\\", (uid,))
    db.execute('DELETE FROM quiz_log WHERE user_id=?', (uid,))
wid = db.execute(\\\"SELECT id FROM words WHERE word='flurbo'\\\").fetchone()
if wid:
    db.execute('DELETE FROM word_senses WHERE word_id=?', (wid[0],))
    db.execute('DELETE FROM words WHERE id=?', (wid[0],))
db.commit()
"`);
  const browser = await chromium.launch({ executablePath: '/opt/google/chrome/chrome',
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--autoplay-policy=no-user-gesture-required'] });
  const page = await browser.newPage();
  await page.goto(BASE);

  // 登录
  await page.fill('input[placeholder*="用户名"], input[type=text]', 'e2e_lib2').catch(()=>{});
  const inputs = await page.locator('input').all();
  await inputs[0].fill('e2e_lib2');
  await inputs[1].fill('lib123');
  await page.click('button:has-text("进入")');
  await page.waitForSelector('text=制定你的计划', { timeout: 8000 });
  ok(true, 'junior 登录直达 setup');

  // 进入词库视图
  await page.click('button:has-text("自选词库")');
  await page.waitForSelector('text=选择词库', { timeout: 5000 });
  ok(true, '词库列表页');
  await page.click('button:has-text("新建词单")');

  // ① 精确命中
  await page.fill('.lib-input-row input', 'apple');
  await page.click('button:has-text("添加")');
  await page.waitForSelector('.ok-line', { timeout: 5000 });
  ok((await page.textContent('.ok-line')).includes('apple'), 'apple 精确命中已加');

  // ② 同音词提示（see → sea）
  await page.fill('.lib-input-row input', 'see');
  await page.click('button:has-text("添加")');
  await page.waitForSelector('.alt-row', { timeout: 5000 });
  const altText = await page.textContent('.alt-row');
  ok(altText.includes('sea'), 'see 同音词提示 sea');
  await page.click('.alt-row .btn.chip');   // 换成第一个同音词
  await page.waitForTimeout(300);
  const chips1 = await page.textContent('.lib-words');
  ok(chips1.includes('sea'), '同音替换为 sea 生效');

  // ③ 中文反查候选
  await page.fill('.lib-input-row input', '苹果');
  await page.click('button:has-text("添加")');
  await page.waitForSelector('.lib-lookup-result .btn.chip', { timeout: 5000 });
  const candText = await page.textContent('.lib-lookup-result');
  ok(candText.includes('apple'), '中文反查给 apple 候选');
  await page.click('.lib-lookup-result .btn.chip');
  await page.waitForTimeout(300);

  // ④ 库外词 pending
  await page.fill('.lib-input-row input', 'flurbo');
  await page.click('button:has-text("添加")');
  await page.waitForSelector('.pend-line', { timeout: 5000 });
  ok((await page.textContent('.lib-words')).includes('flurbo'), '库外词标待生成');

  // 保存
  await page.fill('input[placeholder*="教材词"]', '冒烟词单');
  await page.click('button:has-text("保存词单")');
  await page.waitForSelector('.lib-item', { timeout: 5000 });
  const itemText = await page.textContent('.lib-item');
  ok(itemText.includes('冒烟词单') && itemText.includes('内容准备中'), '词单已存且显示准备中');
  const chooseDisabled = await page.locator('.lib-item button:has-text("选用")').count();
  ok(chooseDisabled === 0, 'pending 库无选用按钮');

  // 数据库造 senses → 刷新 → 可用 → 选用 → 建计划
  execSync(`python3 -c "
import sqlite3
db = sqlite3.connect('/tmp/wl-smoke.db')
wid = db.execute(\\\"SELECT id FROM words WHERE word='flurbo'\\\").fetchone()[0]
db.execute(\\\"INSERT INTO word_senses(word_id, sense_order, pos, definition_zh, example_en) VALUES(?,1,'n.','冒烟词','a flurbo test')\\\", (wid,))
db.commit()
"`);
  await page.reload();
  await page.waitForTimeout(1500);
  if (await page.locator('button:has-text("进入")').count()) {   // 会话已失效才需重登
    const ins = await page.locator('input').all();
    await ins[0].fill('e2e_lib2'); await ins[1].fill('lib123');
    await page.click('button:has-text("进入")');
  }
  await page.waitForSelector('text=制定你的计划', { timeout: 8000 });
  await page.click('button:has-text("自选词库")');
  await page.waitForSelector('.lib-item', { timeout: 5000 });
  ok((await page.textContent('.lib-item')).includes('可用'), '内容补齐后库变可用');
  await page.click('.lib-item button:has-text("选用")');
  await page.waitForSelector('text=制定你的计划', { timeout: 5000 });
  ok((await page.textContent('section')).includes('已选：冒烟词单'), 'setup 显示已选词库');

  // 建计划（3 词 → target=10 会被池截到 3）
  await page.fill('input[type=number]', '10');
  await page.click('button:has-text("开始！")');
  await page.waitForSelector('.today-wrap', { timeout: 10000 });
  await page.waitForTimeout(1500);
  const body = await page.textContent('body');

  // 后端验证队列 ⊆ 库
  const out = execSync(`python3 -c "
import sqlite3, json
db = sqlite3.connect('/tmp/wl-smoke.db'); db.row_factory = sqlite3.Row
uid = db.execute(\\\"SELECT id FROM users WHERE username='e2e_lib2'\\\").fetchone()['id']
lib_words = set(r['word'] for r in db.execute('''SELECT w.word FROM library_words lw JOIN words w ON w.id=lw.word_id WHERE lw.library_id=(SELECT library_id FROM study_plans WHERE user_id=? AND status='active')''', (uid,)))
plan_words = [r['word'] for r in db.execute('''SELECT w.word FROM plan_words p JOIN words w ON w.id=p.word_id
  JOIN study_plans sp ON sp.id=p.plan_id WHERE sp.user_id=? AND sp.status='active' ORDER BY p.seq''', (uid,))]
print(json.dumps({'lib': sorted(lib_words), 'plan': plan_words}))
"`).toString();
  const v = JSON.parse(out);
  ok(v.plan.length === 3, `计划词数=3（实际 ${v.plan.length}）`);
  ok(v.plan.every(w => v.lib.includes(w)), `计划词⊆库: ${v.plan}`);
  ok(v.plan.includes('flurbo'), '库外补齐词也在池内');

  console.log(`\n=== ${pass} pass / ${fail} fail ===`);
  await browser.close();
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error('SMOKE CRASH', e); process.exit(2); });
