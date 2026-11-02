/* v3.1 全链路冒烟：录库外词→保存→立即可学→题链完整→（管线精修后升级）→编辑 */
const { chromium } = require('/usr/local/lib/node_modules/@playwright/cli/node_modules/playwright-core');
const { execSync } = require('child_process');
let pass = 0, fail = 0;
const ok = (cond, name, extra) => {
  if (cond) { pass++; console.log('  ✓', name, extra || ''); }
  else { fail++; console.log('  ✗ FAIL', name, extra || ''); }
};
const q = (sql) => execSync(`python3 -c "
import sqlite3, json
db = sqlite3.connect('/tmp/wl-v31.db'); db.row_factory = sqlite3.Row
print(json.dumps([dict(r) for r in db.execute(${JSON.stringify(sql)})]))
"`).toString().trim();

(async () => {
  const browser = await chromium.launch({ executablePath: '/opt/google/chrome/chrome',
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--autoplay-policy=no-user-gesture-required'] });
  const page = await browser.newPage();
  page.on('pageerror', e => console.log('PAGEERROR:', e.message));
  await page.goto('http://localhost:8091');
  await page.waitForTimeout(800);
  const ins = await page.locator('input').all();
  await ins[0].fill('test'); await ins[1].fill('t123');
  await page.click('button:has-text("进入")');
  await page.waitForTimeout(1500);
  // test 是 standard 5A 已确认 → 直接 setup
  await page.click('button:has-text("自选词库")');
  await page.waitForSelector('text=选择词库', { timeout: 5000 });
  await page.click('button:has-text("新建词单")');

  // 库外词 + 库内词
  await page.fill('.lib-input-row input', 'brush my teeth');
  await page.click('button:has-text("添加")');
  await page.waitForSelector('.pend-line', { timeout: 5000 });
  ok(true, '库外词 brush my teeth 提示自动补齐');
  await page.fill('.lib-input-row input', 'apple');
  await page.click('button:has-text("添加")');
  await page.waitForSelector('.ok-line', { timeout: 5000 });
  await page.fill('.lib-input-row input', 'zebra');
  await page.click('button:has-text("添加")');
  await page.waitForTimeout(600);
  await page.fill('input[placeholder*="教材词"]', 'v31冒烟词单');
  await page.click('button:has-text("保存词单")');
  await page.waitForSelector('.lib-item:has-text("v31冒烟词单")', { timeout: 5000 });
  const itemText = await page.textContent('.lib-item:has-text("v31冒烟词单")');
  ok(itemText.includes('可学'), '保存后立即"可学"（不阻塞）', itemText.match(/\d+ 词 · (.*)/)?.[1]);
  ok(await page.locator('.lib-item:has-text("v31冒烟词单") button:has-text("选用")').count() > 0, '选用按钮可点');

  // 立即建计划
  await page.click('.lib-item:has-text("v31冒烟词单") button:has-text("选用")');
  await page.waitForSelector('text=制定你的计划', { timeout: 5000 });
  await page.fill('input[type=number]', '10');
  await page.click('button:has-text("开始！")');
  await page.waitForSelector('.today-wrap', { timeout: 10000 });
  await page.waitForTimeout(2000);
  // today 队列与题链验证（standard 4 题：word_zh/listen_zh/zh_word/spell）
  const v = JSON.parse(q(`SELECT w.word, q.quiz_type, q.stage FROM plan_words p
    JOIN words w ON w.id=p.word_id JOIN study_plans sp ON sp.id=p.plan_id
    LEFT JOIN word_quiz q ON q.word_id=w.id AND sp.status='active'
    WHERE sp.status='active' AND sp.user_id=(SELECT id FROM users WHERE username='test')`));
  const words = [...new Set(v.map(x => x.word))];
  ok(words.includes('brush my teeth'), '库外词进了计划', words.join(','));
  const mtb = v.filter(x => x.word === 'brush my teeth');
  ok(mtb.length > 0, `brush my teeth 有题（${mtb.length} 道）`);
  // 浏览器侧题链（现算）
  const chain = await page.evaluate(() => {
    const items = window.document.body.textContent;
    return items;
  });
  console.log('  （今日页面加载成功）');
  await page.screenshot({ path: '/tmp/v31-today.png' });

  console.log(`\n=== ${pass} pass / ${fail} fail ===`);
  await browser.close();
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error('CRASH', e); process.exit(2); });
