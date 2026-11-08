/* 真实浏览器 E2E：模拟孩子完整使用旅程（v3.3 重写为当前流程）。
   覆盖：登录/错误密码 → 欢迎引导(年级/目标) → 基础词快测 → 表格挑认识词(免验证) → 建计划 →
   新词卡片(注视期) → 做题中日历锁定 → 进度页 → junior 模式 → D类数据库审计。
   历史注记：v2.x 前为「存量摸底自评+验证」流程（认识/不认识/有点模糊），v3.3 起按现行 UI 重写；
   D类审计收编自已退役的 invariant-test.js（其画像引擎依赖已删除的 vm.screen/vm.spot）。 */
const { chromium } = require('playwright-core');
const { execFileSync } = require('child_process');
const fs = require('fs');

const BASE = process.env.WL_BASE || 'https://words.velen.ltd';
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
for u in ('${STD}', '${JUN}'):
    uid = db.execute("SELECT id FROM users WHERE username=?", (u,)).fetchone()
    if uid:
        uid = uid[0]
        db.execute("DELETE FROM plan_words WHERE plan_id IN (SELECT id FROM study_plans WHERE user_id=?)", (uid,))
        db.execute("DELETE FROM study_plans WHERE user_id=?", (uid,))
        db.execute("DELETE FROM daily_logs WHERE user_id=?", (uid,))
        db.execute("DELETE FROM quiz_log WHERE user_id=?", (uid,))
        db.execute("DELETE FROM user_word_state WHERE user_id=?", (uid,))
        db.execute("DELETE FROM users WHERE id=?", (uid,))
db.execute("INSERT INTO users(username,password_hash,display_name,mode) VALUES(?,?,'Std','standard')", ('${STD}', h))
db.execute("INSERT INTO users(username,password_hash,display_name,mode) VALUES(?,?,'Jun','junior')", ('${JUN}', h))
db.commit()
print('users ready')
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
        db.execute("DELETE FROM quiz_log WHERE user_id=?", (uid,))
        db.execute("DELETE FROM user_word_state WHERE user_id=?", (uid,))
        db.execute("DELETE FROM users WHERE id=?", (uid,))
db.commit()
print('cleaned')
`;

(async () => {
  dockerPy(setupUsers);

  const browser = await chromium.launch({
    executablePath: '/usr/bin/google-chrome',
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--autoplay-policy=no-user-gesture-required'],
  });
  const ctx = await browser.newContext({ viewport: { width: 820, height: 1200 } });
  const page = await ctx.newPage();
  const pageErrors = [];
  page.on('pageerror', e => pageErrors.push(String(e.message)));

  console.log('— 1. 登录页 —');
  await page.goto(BASE, { waitUntil: 'domcontentloaded', timeout: 20000 });
  check('Vue 挂载(logo渲染)', (await page.locator('.logo').textContent()) === '🪐 单词星球');
  check('无未渲染指令残留', !(await page.content()).includes('v-if='));
  await page.screenshot({ path: SHOTS + '/01-login.png' });

  console.log('— 2. 错误密码 —');
  await page.fill('input[placeholder="用户名"]', STD);
  await page.fill('input[placeholder="密码"]', 'wrongpass');
  await page.click('button:has-text("进入")');
  await page.waitForSelector('.err', { timeout: 5000 });
  check('错误提示显示', (await page.locator('.err').textContent()).includes('错误'));

  console.log('— 3. 欢迎引导：年级 → 目标 → 摸底引导 —');
  await page.fill('input[placeholder="密码"]', PW);
  await page.click('button:has-text("进入")');
  await page.waitForSelector('h2:has-text("你好")', { timeout: 8000 });
  await page.click('button:has-text("五年级上")');
  await page.waitForSelector('h2:has-text("你的目标是")');
  await page.click('.goal-card:has-text("稳步前进")');
  await page.click('.day-btn:has-text("60 天")');
  await page.click('button:has-text("下一步")');
  await page.waitForSelector('h2:has-text("先把你会的挑出来")', { timeout: 8000 });
  await page.screenshot({ path: SHOTS + '/02-welcome.png' });

  console.log('— 4. 基础词快测（15 题全对 → 直接通过）—');
  await page.click('button:has-text("开始挑选")');
  await page.waitForSelector('h2:has-text("小热身")', { timeout: 8000 });
  for (let i = 0; i < 120; i++) {
    const st = await page.evaluate(() => {
      const vm = window.__vm;
      const q = vm.curBasicQuiz();
      return { phase: vm.confirm.phase, picked: vm.confirm.basicPicked,
               idx: q ? q.options.indexOf(q.answer) : -1 };
    });
    if (st.phase !== 'basic' || st.idx < 0) break;
    if (st.picked != null) { await page.waitForTimeout(300); continue; }
    await page.locator('.quiz-grid button.opt').nth(Math.max(0, st.idx)).click();
    await page.waitForTimeout(350);
  }
  await page.waitForSelector('h2:has-text("基础很扎实")', { timeout: 10000 });
  check('快测全对 → 基础扎实', true);
  await page.screenshot({ path: SHOTS + '/02b-basic-done.png' });
  await page.click('button:has-text("开始挑新词")');

  console.log('— 5. 表格挑认识词（选2个→取消1个→再选回→翻屏提交）—');
  await page.waitForSelector('h2:has-text("挑出你已经认识的词")', { timeout: 8000 });
  await page.locator('.chip').nth(0).click();
  await page.locator('.chip').nth(1).click();
  await page.waitForTimeout(200);
  let prog = (await page.locator('.progress-line').first().textContent() || '').trim();
  check('已选计数=2', /已选 <b>2<\/b>/.test(prog) || prog.includes('2 个认识'), prog);
  check('选中chip变绿划线', (await page.locator('.chip.known').count()) === 2);
  await page.locator('.chip').nth(1).click();          // 取消一个
  await page.waitForTimeout(200);
  check('再点取消选择', (await page.locator('.chip.known').count()) === 1);
  await page.locator('.chip').nth(1).click();          // 选回
  await page.waitForTimeout(200);
  const knownWords = await page.locator('.chip.known').allTextContents();
  // 翻屏到最后一屏 → 完成挑选（主按钮文案自动切换）
  for (let i = 0; i < 30; i++) {
    if (await page.locator('.confirm-nav button:has-text("完成挑选")').count()) break;
    await page.locator('.confirm-nav button:has-text("下一屏")').click();
    await page.waitForTimeout(150);
  }
  await page.locator('.confirm-nav button:has-text("完成挑选")').click();
  await page.waitForSelector('h2:has-text("挑选完成")', { timeout: 10000 });
  const doneTxt = await page.locator('.card.done-card').textContent();
  check('免验证直达完成(认识N个)', /已经认识 \d+ 个词/.test(doneTxt), doneTxt.slice(0, 60));
  await page.screenshot({ path: SHOTS + '/02c-confirm-done.png' });

  console.log('— 6. 建计划 10词/14天（每日5词）—');
  await page.click('button:has-text("开始制定计划")');
  await page.waitForSelector('h2:has-text("制定你的计划")', { timeout: 8000 });
  const hint = (await page.locator('.hint').filter({ hasText: '每天新学约' }).first().textContent() || '').trim();
  check('每日新词预览', /每天新学约 \d+ 个/.test(hint), hint);
  await page.screenshot({ path: SHOTS + '/03-setup.png' });
  await page.locator('input[type=number]').nth(0).fill('10');
  await page.locator('input[type=number]').nth(1).fill('14');
  await page.click('button:has-text("开始！")');
  await page.waitForSelector('.phase-tag, button:has-text("开始学习")', { timeout: 8000 });
  if (await page.locator('button:has-text("开始学习")').count()) {
    await page.click('button:has-text("开始学习")');   // 声音解锁屏
  }
  await page.waitForSelector('.phase-tag', { timeout: 8000 });
  const tag = await page.locator('.phase-tag').textContent();
  check('新词阶段 1/5', tag.includes('认识新朋友 1 / 5'), tag);
  check('卡片释义渲染', (await page.locator('.learn-card .sense-block, .learn-card .def-zh').count()) >= 1);
  const btnBox = await page.locator('button:has-text("再听一次")').boundingBox();
  check('B8: 发音按钮横排(宽>高)', !!btnBox && btnBox.width > btnBox.height,
        btnBox ? `w=${Math.round(btnBox.width)} h=${Math.round(btnBox.height)}` : 'not found');
  await page.screenshot({ path: SHOTS + '/04-learn.png' });

  console.log('— 7. 学2词推进（注视期 2.5s 自动等待）—');
  for (let i = 0; i < 2; i++) {
    await page.waitForFunction(() => {
      const b = [...document.querySelectorAll('button.btn.primary.big')].pop();
      return b && !b.disabled;
    }, { timeout: 10000 });
    await page.locator('button.btn.primary.big').last().click();
    await page.waitForTimeout(300);
  }
  check('推进到 3/5', (await page.locator('.phase-tag').textContent()).includes('认识新朋友 3 / 5'));

  console.log('— 8. 日历锁定（做题中不可进；渲染断言在 done 测试）—');
  await page.click('.tabbar button:has-text("日历")');
  await page.waitForTimeout(400);
  check('做题中点日历被拦截（日历未渲染）', (await page.locator('.cal-grid').count()) === 0);
  check('拦截提示出现', (await page.locator('.toast').count()) >= 1);
  check('日历按钮置灰', (await page.locator('.tabbar button.locked').count()) === 1);
  await page.screenshot({ path: SHOTS + '/05-calendar-locked.png' });

  console.log('— 9. 进度（做题中不锁）—');
  await page.click('.tabbar button:has-text("进度")');
  await page.waitForSelector('h2:has-text("学习进度")');
  check('进度条渲染', (await page.locator('.bar-fill').count()) >= 1);
  await page.screenshot({ path: SHOTS + '/06-progress.png' });

  console.log('— 10. junior 账号（低年级视角）—');
  await page.click('button:has-text("退出")');
  await page.waitForSelector('input[placeholder="用户名"]');
  await page.fill('input[placeholder="用户名"]', JUN);
  await page.fill('input[placeholder="密码"]', PW);
  await page.click('button:has-text("进入")');
  await page.waitForSelector('h2:has-text("你好")', { timeout: 8000 });
  await page.click('button:has-text("一年级上")');
  await page.waitForSelector('h2:has-text("你的目标是")');
  check('junior 目标档 60 词', (await page.locator('.goal-card').first().textContent()).includes('60'));
  await page.click('.goal-card:has-text("轻松起步")');
  await page.click('button:has-text("下一步")');
  await page.waitForSelector('h2:has-text("制定你的计划")', { timeout: 8000 });   // junior 无摸底直达
  const def = await page.locator('input[type=number]').nth(0).inputValue();
  check('junior 默认=意向60', def === '60', String(def));
  await page.click('button:has-text("开始！")');
  await page.waitForSelector('.phase-tag, button:has-text("开始学习")', { timeout: 8000 });
  if (await page.locator('button:has-text("开始学习")').count()) {
    await page.click('button:has-text("开始学习")');
  }
  await page.waitForSelector('.phase-tag', { timeout: 8000 });
  check('junior 模式类生效', (await page.locator('.app-shell.mode-junior').count()) === 1);
  check('junior 隐藏音标', (await page.locator('.learn-card .phonetic').count()) === 0);
  check('junior 大卡片渲染', (await page.locator('.word-xl').textContent() || '').length > 0);
  await page.screenshot({ path: SHOTS + '/07-junior.png' });

  check('全程无未捕获JS异常', pageErrors.length === 0, pageErrors.slice(0, 2).join(' ; '));

  console.log('— 11. D类 数据库审计（收编自 invariant-test）—');
  dbAudit([STD, JUN]);

  await browser.close();
  dockerPy(cleanup);

  console.log('\n' + (FAILS.length ? '❌ FAILED: ' + FAILS.join(', ') : '✅ 浏览器 E2E 全部通过，截图在 ' + SHOTS));
  process.exit(FAILS.length ? 1 : 0);
})().catch(e => {
  console.error('FATAL:', e.message);
  try { dockerPy(cleanup); } catch (_) {}
  process.exit(2);
});

/* D类：走完 UI 旅程后核对数据库自洽（状态值/组合/计划去重/日志==首学数） */
function dbAudit(users) {
  const out = dockerPy(`import sqlite3, json
db = sqlite3.connect('/app/data/wordslearn.db')
db.row_factory = sqlite3.Row
res = {}
for u in ${JSON.stringify(users)}:
    uid = db.execute("SELECT id FROM users WHERE username=?", (u,)).fetchone()
    if not uid: continue
    uid = uid['id']
    bad_state = db.execute("SELECT COUNT(*) c FROM user_word_state WHERE user_id=? AND state NOT IN ('new','learning','mastered','skipped')", (uid,)).fetchone()['c']
    bad_combo = db.execute("SELECT COUNT(*) c FROM user_word_state WHERE user_id=? AND ((state='mastered' AND next_review_date IS NOT NULL) OR (state='learning' AND (next_review_date IS NULL OR first_learned_date IS NULL)))", (uid,)).fetchone()['c']
    dup_plan = db.execute("SELECT COUNT(*) c FROM (SELECT word_id FROM plan_words p JOIN study_plans sp ON sp.id=p.plan_id WHERE sp.user_id=? GROUP BY word_id HAVING COUNT(*)>1)", (uid,)).fetchone()['c']
    log_vs_state = None
    plan = db.execute("SELECT id FROM study_plans WHERE user_id=? AND status='active'", (uid,)).fetchone()
    if plan:
        logn = db.execute("SELECT COALESCE(new_done,0) n FROM daily_logs WHERE user_id=? AND date=date('now','localtime')", (uid,)).fetchone()
        staten = db.execute("SELECT COUNT(*) c FROM user_word_state WHERE user_id=? AND first_learned_date=date('now','localtime')", (uid,)).fetchone()['c']
        log_vs_state = (logn['n'] if logn else 0) - staten
    res[u] = dict(bad_state=bad_state, bad_combo=bad_combo, dup_plan=dup_plan, log_minus_state=log_vs_state)
print(json.dumps(res))`).trim();
  const audit = JSON.parse(out);
  for (const [u, a] of Object.entries(audit)) {
    check(`D1 ${u} 状态值合法`, a.bad_state === 0, JSON.stringify(a));
    check(`D2 ${u} 状态-日期组合合法`, a.bad_combo === 0);
    check(`D3 ${u} 计划无重复词`, a.dup_plan === 0);
    if (a.log_minus_state !== null) check(`D5 ${u} 日志==首学数`, a.log_minus_state === 0, String(a.log_minus_state));
  }
}
