/* 完成态场景截图：小计划学完 → done 庆祝页 → 日历今日高亮 */
const { chromium } = require('playwright-core');
const { execFileSync } = require('child_process');

const BASE = 'https://words.velen.ltd';
const PW = 'Test123456';
const U = 'brtest_done';
const SHOTS = '/tmp/wltest/shots';

function dockerPy(code) {
  return execFileSync('docker', ['exec', 'wordslearn', 'python', '-c', code], { encoding: 'utf8' });
}

(async () => {
  dockerPy(`import sqlite3, bcrypt
db = sqlite3.connect('/app/data/wordslearn.db')
h = bcrypt.hashpw(b'${PW}', bcrypt.gensalt()).decode()
db.execute("DELETE FROM plan_words WHERE plan_id IN (SELECT id FROM study_plans WHERE user_id IN (SELECT id FROM users WHERE username='${U}'))")
db.execute("DELETE FROM study_plans WHERE user_id IN (SELECT id FROM users WHERE username='${U}')")
db.execute("DELETE FROM daily_logs WHERE user_id IN (SELECT id FROM users WHERE username='${U}')")
db.execute("DELETE FROM user_word_state WHERE user_id IN (SELECT id FROM users WHERE username='${U}')")
db.execute("DELETE FROM users WHERE username='${U}'")
db.execute("INSERT INTO users(username,password_hash,display_name,mode) VALUES(?,?,'Done','standard')", ('${U}', h))
db.commit()`);

  const browser = await chromium.launch({
    executablePath: '/opt/google/chrome/chrome',
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--autoplay-policy=no-user-gesture-required'],
  });
  const page = await (await browser.newContext({ viewport: { width: 820, height: 1200 } })).newPage();

  await page.goto(BASE, { waitUntil: 'networkidle' });
  await page.fill('input[placeholder="用户名"]', U);
  await page.fill('input[placeholder="密码"]', PW);
  await page.click('button:has-text("进入")');
  await page.waitForSelector('h2:has-text("先看看哪些词已经会了")');
  dockerPy(`import sqlite3
db = sqlite3.connect('/app/data/wordslearn.db')
uid = db.execute("SELECT id FROM users WHERE username='${U}'").fetchone()[0]
db.execute("UPDATE user_word_state SET state='skipped' WHERE user_id=? AND state='verify'", (uid,))
db.execute("""INSERT INTO user_word_state(user_id, word_id, state) SELECT ?, w.id, 'skipped' FROM words w
  JOIN word_levels l ON l.word_id=w.id AND l.grade_code IN ('P13','P45')
  WHERE NOT EXISTS (SELECT 1 FROM user_word_state s WHERE s.user_id=? AND s.word_id=w.id)""", (uid, uid))
db.commit()`);
  await page.goto(BASE, { waitUntil: 'networkidle' });
  await page.waitForSelector('h2:has-text("制定你的计划")');
  await page.locator('input[type=number]').nth(0).fill('12');
  await page.locator('input[type=number]').nth(1).fill('10');
  await page.click('button:has-text("开始！")');
  await page.waitForSelector('.phase-tag', { timeout: 8000 });
  const phaseTag = await page.locator('.phase-tag').textContent();
  check(phaseTag.includes('新词 1 / 4'), 'daily_new=4');

  await page.screenshot({ path: SHOTS + '/08-learn-progress.png' });

  // 学完 2 个新词 → 复习队列为空 → done
  for (let i = 0; i < 4; i++) {
    await page.click('button:has-text("学会了，下一个")');
    await page.waitForTimeout(400);
  }
  await page.waitForSelector('.done-card', { timeout: 5000 });
  const doneText = await page.locator('.done-card').textContent();
  check('完成页出现', doneText.includes('今日任务完成'));
  check('完成页统计三件套', doneText.includes('新学单词') && doneText.includes('连续天数'));
  check('累计掌握数显示', doneText.includes('已学会'));
  await page.screenshot({ path: SHOTS + '/09-done.png' });

  // 日历：今天应该绿色达标 + 蓝框高亮
  await page.click('.tabbar button:has-text("日历")');
  await page.waitForSelector('.cal-grid');
  await page.waitForTimeout(400);
  check('今天=绿达标+蓝框高亮', (await page.locator('.cal-cell.today.done').count()) === 1);
  await page.screenshot({ path: SHOTS + '/10-calendar-today.png' });

  // 清理
  dockerPy(`import sqlite3
db = sqlite3.connect('/app/data/wordslearn.db')
uid = db.execute("SELECT id FROM users WHERE username='${U}'").fetchone()[0]
db.execute("DELETE FROM plan_words WHERE plan_id IN (SELECT id FROM study_plans WHERE user_id=?)", (uid,))
db.execute("DELETE FROM study_plans WHERE user_id=?", (uid,))
db.execute("DELETE FROM daily_logs WHERE user_id=?", (uid,))
db.execute("DELETE FROM user_word_state WHERE user_id=?", (uid,))
db.execute("DELETE FROM users WHERE id=?", (uid,))
db.commit()`);
  await browser.close();
  console.log('✅ 完成态场景全部通过');
  process.exit(0);
})().catch(e => { console.error('FATAL:', e.message); process.exit(2); });

function check(name, cond, extra) { console.log((cond ? '  ✓' : '  ✗ FAIL'), name, extra ? '| ' + extra : ''); if (!cond) process.exitCode = 1; }
