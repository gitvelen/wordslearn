/* 真实浏览器 E2E：模拟孩子完整使用旅程。覆盖 Vue 渲染、双模式、交互、截图 */
const { chromium } = require('playwright-core');
const { execFileSync } = require('child_process');
const fs = require('fs');

const BASE = 'https://words.velen.ltd';
const PW = 'Test123456';
const STD = 'brtest_std';
const JUN = 'brtest_jun';
const SHOTS = '/tmp/wltest/shots';
fs.mkdirSync(SHOTS, { recursive: true });

function dockerPy(code) {
  return execFileSync('docker', ['exec', 'wordslearn', 'python', '-c', code], { encoding: 'utf8' });
}

let FAILS = [];
function check(name, cond, extra = '') {
  console.log((cond ? '  ✓' : '  ✗ FAIL'), name, extra ? '| ' + extra : '');
  if (!cond) FAILS.push(name);
}

const setupUsers = `
import sqlite3, bcrypt
db = sqlite3.connect('/app/data/wordslearn.db')
h = bcrypt.hashpw(b'${PW}', bcrypt.gensalt()).decode()
for u, m in (('${STD}', 'standard'), ('${JUN}', 'junior')):
    db.execute("DELETE FROM plan_words WHERE plan_id IN (SELECT id FROM study_plans WHERE user_id IN (SELECT id FROM users WHERE username=?))", (u,))
    db.execute("DELETE FROM study_plans WHERE user_id IN (SELECT id FROM users WHERE username=?)", (u,))
    db.execute("DELETE FROM daily_logs WHERE user_id IN (SELECT id FROM users WHERE username=?)", (u,))
    db.execute("DELETE FROM user_word_state WHERE user_id IN (SELECT id FROM users WHERE username=?)", (u,))
    db.execute("DELETE FROM users WHERE username=?", (u,))
    db.execute("INSERT INTO users(username, password_hash, display_name, mode) VALUES(?,?,?,?)", (u, h, u, m))
db.commit()
print('users ready')
`;

const finishScreening = `
import sqlite3
db = sqlite3.connect('/app/data/wordslearn.db')
uid = db.execute("SELECT id FROM users WHERE username='${STD}'").fetchone()[0]
db.execute("UPDATE user_word_state SET state='skipped' WHERE user_id=? AND state='verify'", (uid,))
db.execute("""INSERT INTO user_word_state(user_id, word_id, state)
    SELECT ?, w.id, 'skipped' FROM words w
    JOIN word_levels l ON l.word_id=w.id AND l.grade_code IN ('P13','P45')
    WHERE NOT EXISTS (SELECT 1 FROM user_word_state s WHERE s.user_id=? AND s.word_id=w.id)""", (uid, uid))
db.commit()
print('screening finished')
`;

const cleanup = `
import sqlite3
db = sqlite3.connect('/app/data/wordslearn.db')
for u in ('${STD}', '${JUN}'):
    row = db.execute("SELECT id FROM users WHERE username=?", (u,)).fetchone()
    if row:
        uid = row[0]
        db.execute("DELETE FROM plan_words WHERE plan_id IN (SELECT id FROM study_plans WHERE user_id=?)", (uid,))
        db.execute("DELETE FROM study_plans WHERE user_id=?", (uid,))
        db.execute("DELETE FROM daily_logs WHERE user_id=?", (uid,))
        db.execute("DELETE FROM user_word_state WHERE user_id=?", (uid,))
        db.execute("DELETE FROM users WHERE id=?", (uid,))
db.commit()
print('cleaned')
`;

(async () => {
  dockerPy(setupUsers);

  const browser = await chromium.launch({
    executablePath: '/opt/google/chrome/chrome',
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--autoplay-policy=no-user-gesture-required'],
  });
  const ctx = await browser.newContext({ viewport: { width: 820, height: 1200 } });
  const page = await ctx.newPage();
  const pageErrors = [];
  page.on('pageerror', e => pageErrors.push(String(e.message)));

  console.log('— 1. 登录页 —');
  await page.goto(BASE, { waitUntil: 'networkidle', timeout: 20000 });
  check('Vue 挂载(logo渲染)', (await page.locator('.logo').textContent()) === '🪐 单词星球');
  check('无未渲染指令残留', !(await page.content()).includes('v-if='));
  await page.screenshot({ path: SHOTS + '/01-login.png' });

  console.log('— 2. 错误密码 —');
  await page.fill('input[placeholder="用户名"]', STD);
  await page.fill('input[placeholder="密码"]', 'wrongpass');
  await page.click('button:has-text("进入")');
  await page.waitForSelector('.err', { timeout: 5000 });
  check('错误提示显示', (await page.locator('.err').textContent()).includes('错误'));

  console.log('— 3. 登录 → 摸底 —');
  await page.fill('input[placeholder="密码"]', PW);
  await page.click('button:has-text("进入")');
  await page.waitForSelector('h2:has-text("先看看哪些词已经会了")', { timeout: 8000 });
  await page.waitForTimeout(600);
  const prog = (await page.locator('.progress-line').first().textContent() || '').trim();
  check('摸底总进度显示', /存量摸底 \d+ \/ \d+/.test(prog), prog);
  const word1 = await page.locator('.word-xl').textContent();
  check('发音按钮存在', (await page.locator('button.round').count()) >= 1);
  await page.screenshot({ path: SHOTS + '/02-screening.png' });

  console.log('— 4. 自评交互 —');
  await page.click('button:has-text("认识")');
  await page.waitForTimeout(250);
  await page.click('button:has-text("不认识")');
  await page.waitForTimeout(250);
  await page.click('button:has-text("有点模糊")');
  await page.waitForTimeout(400);
  check('自评推进(切词)', (await page.locator('.word-xl').textContent()) !== word1);

  console.log('— 5. 补完摸底(SQL) → 计划设置 —');
  dockerPy(finishScreening);
  await page.goto(BASE, { waitUntil: 'networkidle' });
  await page.waitForSelector('h2:has-text("制定你的计划")', { timeout: 8000 });
  const hint = (await page.locator('.hint').first().textContent() || '').trim();
  check('每日新词预览', /每天新学约 \d+ 个/.test(hint), hint);
  await page.screenshot({ path: SHOTS + '/03-setup.png' });

  console.log('— 6. 建计划 100词/14天 —');
  await page.locator('input[type=number]').nth(0).fill('100');
  await page.locator('input[type=number]').nth(1).fill('14');
  await page.click('button:has-text("开始！")');
  await page.waitForSelector('.phase-tag', { timeout: 8000 });
  const tag = await page.locator('.phase-tag').textContent();
  check('新词阶段 1/15', tag.includes('新词 1 / 15'), tag);
  check('卡片释义渲染', (await page.locator('.def-zh').textContent() || '').length > 0);
  const btnBox = await page.locator('button:has-text("再听一次")').boundingBox();
  check('B8: 发音按钮横排(宽>高)', !!btnBox && btnBox.width > btnBox.height,
        btnBox ? `w=${Math.round(btnBox.width)} h=${Math.round(btnBox.height)}` : 'not found');
  await page.screenshot({ path: SHOTS + '/04-learn.png' });

  console.log('— 7. 学2词推进 —');
  await page.click('button:has-text("学会了，下一个")');
  await page.waitForTimeout(350);
  await page.click('button:has-text("学会了，下一个")');
  await page.waitForTimeout(350);
  check('推进到 3/15', (await page.locator('.phase-tag').textContent()).includes('新词 3 / 15'));

  console.log('— 8. 日历 —');
  await page.click('.tabbar button:has-text("日历")');
  await page.waitForSelector('.cal-grid');
  await page.waitForTimeout(400);
  check('今日格子有记录', (await page.locator('.cal-cell.done, .cal-cell.part').count()) >= 1);
  await page.screenshot({ path: SHOTS + '/05-calendar.png' });

  console.log('— 9. 进度 —');
  await page.click('.tabbar button:has-text("进度")');
  await page.waitForSelector('h2:has-text("学习进度")');
  check('进度条渲染', (await page.locator('.bar-fill').count()) >= 1);
  await page.screenshot({ path: SHOTS + '/06-progress.png' });

  console.log('— 10. junior 账号（mido 视角）—');
  await page.click('button:has-text("退出")');
  await page.waitForSelector('input[placeholder="用户名"]');
  await page.fill('input[placeholder="用户名"]', JUN);
  await page.fill('input[placeholder="密码"]', PW);
  await page.click('button:has-text("进入")');
  await page.waitForSelector('h2:has-text("制定你的计划")', { timeout: 8000 });
  const def = await page.locator('input[type=number]').nth(0).inputValue();
  check('junior 默认150词', def === '150', String(def));
  await page.click('button:has-text("开始！")');
  await page.waitForSelector('.phase-tag', { timeout: 8000 });
  check('junior 模式类生效', (await page.locator('.app-shell.mode-junior').count()) === 1);
  check('junior 隐藏音标', (await page.locator('.learn-card .phonetic').count()) === 0);
  check('junior 大卡片渲染', (await page.locator('.word-xl').textContent() || '').length > 0);
  await page.screenshot({ path: SHOTS + '/07-junior.png' });

  check('全程无未捕获JS异常', pageErrors.length === 0, pageErrors.slice(0, 2).join(' ; '));

  await browser.close();
  dockerPy(cleanup);

  console.log('\n' + (FAILS.length ? '❌ FAILED: ' + FAILS.join(', ') : '✅ 浏览器 E2E 全部通过，截图在 ' + SHOTS));
  process.exit(FAILS.length ? 1 : 0);
})().catch(e => {
  console.error('FATAL:', e.message);
  try { dockerPy(cleanup); } catch (_) {}
  process.exit(2);
});
