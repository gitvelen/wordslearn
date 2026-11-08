/* 完成态全程旅程（v3.3 重写）：建号(跳过摸底) → 建计划(10词/17天=每日2词) → 做题中锁定断言 →
   通用队列驱动学到完成 → done 庆祝页 → 日历今日高亮 → 当日报告双层 + 新词手风琴。
   历史注记：旧版等待「先看看哪些词已经会了」自评流程，v2.x 改版后失效；v3.3 起按当前流程重写。 */
const { chromium } = require('playwright-core');
const { execFileSync } = require('child_process');

const BASE = process.env.WL_BASE || 'https://words.velen.ltd';
const PW = 'Test123456';
const U = 'brtest_done';
const SHOTS = '/tmp/wltest/shots';

function dockerPy(code) {
  return execFileSync('docker', ['exec', 'wordslearn', 'python', '-c', code], { encoding: 'utf8' });
}

let FAILS = [];
function check(name, cond, extra = '') {
  console.log((cond ? '  ✓' : '  ✗ FAIL'), name, extra ? '| ' + extra : '');
  if (!cond) FAILS.push(name);
}

(async () => {
  dockerPy(`import sqlite3, bcrypt
db = sqlite3.connect('/app/data/wordslearn.db')
h = bcrypt.hashpw(b'${PW}', bcrypt.gensalt()).decode()
db.execute("DELETE FROM plan_words WHERE plan_id IN (SELECT id FROM study_plans WHERE user_id IN (SELECT id FROM users WHERE username='${U}'))")
db.execute("DELETE FROM study_plans WHERE user_id IN (SELECT id FROM users WHERE username='${U}')")
db.execute("DELETE FROM daily_logs WHERE user_id IN (SELECT id FROM users WHERE username='${U}')")
db.execute("DELETE FROM quiz_log WHERE user_id IN (SELECT id FROM users WHERE username='${U}')")
db.execute("DELETE FROM user_word_state WHERE user_id IN (SELECT id FROM users WHERE username='${U}')")
db.execute("DELETE FROM users WHERE username='${U}'")
db.execute("INSERT INTO users(username,password_hash,display_name,mode,grade,confirm_done,basic_quiz_done,screening_skipped) VALUES(?,?,'Done','standard','5A',1,1,1)", ('${U}', h))
db.commit()`);

  const browser = await chromium.launch({
    executablePath: '/opt/google/chrome/chrome',
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--autoplay-policy=no-user-gesture-required'],
  });
  const page = await (await browser.newContext({ viewport: { width: 820, height: 1200 } })).newPage();

  console.log('— 1. 登录 → 建计划（10词/17天 → 每日2词）—');
  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  await page.fill('input[placeholder="用户名"]', U);
  await page.fill('input[placeholder="密码"]', PW);
  await page.click('button:has-text("进入")');
  await page.waitForSelector('h2:has-text("制定你的计划")', { timeout: 8000 });
  await page.locator('input[type=number]').nth(0).fill('10');
  await page.locator('input[type=number]').nth(1).fill('17');
  await page.click('button:has-text("开始！")');
  await page.waitForSelector('.phase-tag, button:has-text("开始学习")', { timeout: 8000 });
  if (await page.locator('button:has-text("开始学习")').count()) {
    await page.click('button:has-text("开始学习")');   // 声音解锁屏
  }
  await page.waitForSelector('.phase-tag', { timeout: 8000 });
  const phaseTag = await page.locator('.phase-tag').textContent();
  check('每日新词=2', /认识新朋友 1 \/ 2/.test(phaseTag), phaseTag);
  await page.screenshot({ path: SHOTS + '/08-learn-progress.png' });

  console.log('— 2. 做题中锁定（v3.3 专注闸）—');
  await page.click('.tabbar button:has-text("日历")');
  await page.waitForTimeout(400);
  check('做题中点日历被拦截（日历未渲染）', (await page.locator('.cal-grid').count()) === 0);
  check('拦截提示出现', (await page.locator('.toast').count()) >= 1);
  check('日历按钮置灰', (await page.locator('.tabbar button.locked').count()) === 1);
  await page.screenshot({ path: SHOTS + '/08b-calendar-locked.png' });

  console.log('— 3. 通用队列驱动：答题直到完成 —');
  let guard = 0;
  while (guard++ < 150) {
    await page.waitForFunction(() => {
      const vm = window.__vm, w = vm.curItem;
      if (!w) return true;                                   // 完成屏
      if (w.type === 'newcard' || w.type === 'gate' || vm.learnIntro) return true;
      if (w.type === 'warmup' && !w.item.quiz) return true;
      return !vm.today.answered;                             // 等到可作答
    }, { timeout: 20000 }).catch(() => {});
    const st = await page.evaluate(() => {
      const vm = window.__vm, w = vm.curItem;
      if (!w) return { t: 'done' };
      if (vm.learnIntro) return { t: 'intro' };
      if (w.type === 'newcard') return { t: 'card' };
      if (w.type === 'gate') return { t: 'gate' };
      if (w.type === 'warmup' && !w.item.quiz) return { t: 'next' };
      return { t: 'quiz', type: w.item.quiz.quiz_type, answer: w.item.quiz.answer };
    });
    if (st.t === 'done') break;
    if (st.t === 'intro') { await page.click('button:has-text("开始学习")'); continue; }
    if (st.t === 'card') {
      await page.waitForFunction(() => {
        const b = [...document.querySelectorAll('button.btn.primary.big')].pop();
        return b && !b.disabled;                              // 注视期 2.5s 解锁
      }, { timeout: 10000 });
      await page.locator('button.btn.primary.big').last().click();
    } else if (st.t === 'gate') {
      await page.click('button:has-text("下一关")');           // 全对路径
    } else if (st.t === 'next') {
      await page.click('button:has-text("继续")');
    } else if (st.t === 'quiz' && st.type === 'spell') {
      await page.evaluate(ans => window.__vm.spellType(ans), st.answer);
      await page.click('button:has-text("提交")');
    } else {
      await page.locator('.quiz-grid button').filter({ hasText: st.answer }).first().click();
    }
    await page.waitForTimeout(200);
  }
  await page.waitForSelector('.done-card', { timeout: 8000 });
  const doneText = await page.locator('.done-card').textContent();
  check('完成页出现', doneText.includes('今日任务完成'));
  check('完成页统计三件套', doneText.includes('新学单词') && doneText.includes('连续天数'));
  check('累计掌握数显示', doneText.includes('已学会'));
  check('完成后日历按钮不再置灰', (await page.locator('.tabbar button.locked').count()) === 0);
  await page.screenshot({ path: SHOTS + '/09-done.png' });

  console.log('— 4. 日历：今天绿达标 + 蓝框 —');
  await page.click('.tabbar button:has-text("日历")');
  await page.waitForSelector('.cal-grid');
  await page.waitForTimeout(400);
  check('今天=绿达标+蓝框高亮', (await page.locator('.cal-cell.today.done').count()) === 1);
  await page.screenshot({ path: SHOTS + '/10-calendar-today.png' });

  console.log('— 5. v3.3 当日报告：完成后今天可点 → 双层 + 手风琴 —');
  await page.locator('.cal-cell.today').click();
  await page.waitForSelector('.rep-card', { timeout: 5000 });
  const repTxt = await page.locator('.rep-card').textContent();
  check('报告概览渲染（日期/得分/统计）',
        repTxt.includes('新学单词') && repTxt.includes('分'));
  check('今日已完成 → 得分已结算', /⭐ \d+ 分/.test(repTxt), repTxt.match(/⭐[^🔥]*/)?.[0]);
  check('家长详情默认折叠', (await page.locator('.rep-parent-body:visible').count()) === 0);
  await page.locator('.rep-word-row').first().click();
  await page.waitForSelector('.rep-word-detail', { timeout: 5000 });
  check('新词手风琴展开释义', (await page.locator('.rep-word-detail .sense-block, .rep-word-detail .def-zh').count()) >= 1);
  await page.locator('.rep-word-row').first().click();
  await page.waitForTimeout(200);
  check('再次点击收起', (await page.locator('.rep-word-detail').count()) === 0);
  await page.locator('.rep-parent button').click();
  await page.waitForTimeout(300);
  check('家长详情可展开', (await page.locator('.rep-parent-body:visible').count()) === 1);
  await page.screenshot({ path: SHOTS + '/11-report.png', fullPage: true });

  // 清理
  dockerPy(`import sqlite3
db = sqlite3.connect('/app/data/wordslearn.db')
uid = db.execute("SELECT id FROM users WHERE username='${U}'").fetchone()[0]
db.execute("DELETE FROM plan_words WHERE plan_id IN (SELECT id FROM study_plans WHERE user_id=?)", (uid,))
db.execute("DELETE FROM study_plans WHERE user_id=?", (uid,))
db.execute("DELETE FROM daily_logs WHERE user_id=?", (uid,))
db.execute("DELETE FROM quiz_log WHERE user_id=?", (uid,))
db.execute("DELETE FROM user_word_state WHERE user_id=?", (uid,))
db.execute("DELETE FROM users WHERE id=?", (uid,))
db.commit()`);
  await browser.close();
  if (FAILS.length) { console.log(`✗ ${FAILS.length} 项失败: ${FAILS.join('、')}`); process.exit(1); }
  console.log('✅ 完成态场景全部通过');
  process.exit(0);
})().catch(e => { console.error('FATAL:', e.message); process.exit(2); });
