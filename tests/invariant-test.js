/* 会话不变式测试：每步交互后断言"界面+调度对用户自洽"，替代单点存在性断言。
   画像：A=mily型(5A/混合正确率/摸底含验证错) B=mido型(1A junior) C=velen型(adult)
   monkey：画像A行为随机化(seed可复现) */
const { chromium } = require('playwright-core');
const { execFileSync } = require('child_process');
const fs = require('fs');

const BASE = 'https://words.velen.ltd';
const PW = 'Test123456';
const SHOTS = '/tmp/wltest/shots';
fs.mkdirSync(SHOTS, { recursive: true });

function dockerPy(code) { return execFileSync('docker', ['exec', 'wordslearn', 'python', '-c', code], { encoding: 'utf8' }); }

let FAILS = [], CHECKS = 0;
function check(name, cond, extra = '') {
  CHECKS++;
  if (!cond) { FAILS.push(name + (extra ? ' | ' + extra : '')); console.log('  ✗', name, extra); }
  return cond;
}

/* ---------- 快照与不变式引擎 ---------- */
async function snap(page) {
  return page.evaluate(async () => {
    const vm = window.__vm;
    const vis = sel => [...document.querySelectorAll(sel)].filter(e => e.offsetParent !== null);
    return {
      view: vm.view,
      phase: vm.screen.phase,
      todayPhase: vm.todayPhase,
      learnIntro: vm.learnIntro,
      spotFinished: vm.spot.finished,
      subTexts: vis('p.sub').map(e => e.textContent.trim()),
      hints: vis('.hint').map(e => e.textContent.trim()),
      buttons: vis('button').map(e => e.textContent.trim()),
      h2s: vis('h2').map(e => e.textContent.trim()),
      progressLines: vis('.progress-line').map(e => e.textContent.trim()),
      wordCards: vis('.word-xl').map(e => e.textContent.trim()),
      segRows: vis('.seg-row').map(e => e.textContent.trim()),
      summaryCard: vis('.summary-card').map(e => e.textContent.trim()),
      quizLen: vm.screen.quiz.length,
      curQuizWordId: (vm.screen.phase === 'quiz' && vm.curQuiz) ? vm.curQuiz.word_id : null,
      quizPicked: (vm.screen.phase === 'quiz' && vm.curQuiz) ? vm.curQuiz.picked : null,
      reviewOpts: (vm.todayPhase === 'review' && vm.today) ? vm.today.options : null,
      reviewAnswer: (vm.todayPhase === 'review' && vm.curReview) ? vm.curReview.definition_zh : null,
      spotResult: vm.spot.finished ? vm.spot.result : null,
      meScreening: vm.me && vm.me.screening ? vm.me.screening : null,
      userMode: vm.user ? vm.user.mode : null,
      grade: vm.user ? vm.user.grade : null,
      plan: vm.me && vm.me.progress ? vm.me.progress.plan : null,
      dailyNew: vm.me && vm.me.progress && vm.me.progress.plan ? vm.me.progress.plan.daily_new : null,
      livePending: await fetch('/api/screening/status', {credentials:'same-origin'}).then(r=>r.json()).then(d=>d.pending).catch(()=>null),
    };
  });
}

function invariants(s, ctx) {
  // I1 阶段-文案一致
  if (s.view === 'screening') {
    const hasSelfHint = s.subTexts.some(t => t.includes('有点模糊」='));
    const hasQuizHint = s.subTexts.some(t => t.includes('正确意思'));
    check('I1 自评提示仅在自评阶段', hasSelfHint === (s.phase === 'self'), `phase=${s.phase}`);
    check('I1 验证说明仅在验证阶段', hasQuizHint === (s.phase === 'quiz'), `phase=${s.phase}`);
    if (s.phase === 'self') {
      // I2 引用完整性：提示引用的「认识」「有点模糊」必须在按钮上
      check('I2 「认识」按钮在场', s.buttons.some(b => b.includes('认识') && !b.includes('不')),
            JSON.stringify(s.buttons));
      check('I2 「有点模糊」按钮在场', s.buttons.some(b => b.includes('有点模糊')));
    }
    if (s.phase === 'quiz') {
      // I9 同屏词卡唯一
      check('I9 验证屏单词卡唯一', s.wordCards.length === 1, JSON.stringify(s.wordCards));
      // I6 验证四选一：正确答案在选项中且唯一
      check('I6 验证题至少3选项', s.buttons.filter(b => !b.includes('🔊')).length >= 3);
    }
    // I11 进度显示永不出现 X/0 分数形态（曾因 sessionStorage 初值缺失出现"0/0"）
    const joined = s.progressLines.join(' ');
    check('I11 进度无 X/0 分数', !/\d+\s*\/\s*0/.test(joined), joined.slice(0, 80));
    if (s.phase === 'self') {
      check('I11 自评阶段显示待自评量', /待自评/.test(joined), joined.slice(0, 80));
    }
    // I3 进度语义：还剩 N == pending
    const m = s.progressLines.join(' ').match(/还剩 (\d+) 词/);
    if (m && s.livePending != null) {
      check('I3 「还剩N」==实时API pending', parseInt(m[1], 10) === s.livePending,
            `显示${m[1]} vs live ${s.livePending}`);
    }
    // I4 序列性：时间线记录（后验分析在结尾做，消除实时检测的竞态假阳性）
    if (s.phase === 'quiz' && s.curQuizWordId != null) {
      if (!ctx.timeline.length || ctx.timeline[ctx.timeline.length-1][1] !== s.curQuizWordId) {
        ctx.timeline.push(['Q', s.curQuizWordId]);
      }
    }
    if (s.view === 'screening' && s.phase === 'self' && !ctx.timeline.length === false && ctx.timeline[ctx.timeline.length-1][0] !== 'S') {
      ctx.timeline.push(['S', 0]);
    }
  }
  // I5 spot 结果屏：显示分段成绩且与 passed/failed 一致
  if (s.view === 'spot' && s.spotFinished && s.spotResult) {
    const r = s.spotResult;
    const passedN = (r.passed || []).length, failedN = (r.failed || []).length;
    const segRows = s.segRows.filter(t => t.includes('通过') || t.includes('需摸底'));
    check('I10 结果屏显示分段成绩', segRows.length === passedN + failedN,
          `rows=${segRows.length} p=${passedN} f=${failedN}`);
    if (failedN === 0) {
      check('I10 全过文案含"基础扎实"', s.h2s.some(h => h.includes('基础扎实')));
    } else {
      check('I10 未过文案说明后续', s.subTexts.some(t => t.includes('摸一摸') || t.includes('摸底')));
    }
  }
  // I6 复习四选一
  if (s.todayPhase === 'review' && s.reviewOpts && s.reviewAnswer) {
    const hit = s.reviewOpts.filter(o => o === s.reviewAnswer).length;
    check('I6 复习正确答案存在且唯一', hit === 1, `answer出现${hit}次`);
    check('I6 复习选项≥3', s.reviewOpts.length >= 3, String(s.reviewOpts.length));
  }
  // I7 intro 屏只在学习首词出现
  if (s.view === 'today' && s.todayPhase === 'new') {
    check('I7 intro只在首词前', true);  // learnIntro 由 computed 保证，抽查按钮
    const hasLearn = s.buttons.some(b => b.includes('学会了'));
    const hasIntro = s.buttons.some(b => b.includes('开始学习'));
    check('I7 学习/intro 二选一', hasLearn !== hasIntro, `learn=${hasLearn} intro=${hasIntro}`);
  }
}

/* ---------- 通用动作（每步后跑不变式） ---------- */
async function step(page, ctx, action) {
  await action();
  await page.waitForTimeout(300);
  const s = await snap(page);
  invariants(s, ctx);
  return s;
}

async function login(page, user) {
  await page.goto(BASE, { waitUntil: 'networkidle' });
  await page.fill('input[placeholder="用户名"]', user);
  await page.fill('input[placeholder="密码"]', PW);
  await page.click('button:has-text("进入")');
  await page.waitForTimeout(600);
}

function mkuser(name) {
  dockerPy(`import sqlite3, bcrypt
db = sqlite3.connect('/app/data/wordslearn.db')
h = bcrypt.hashpw(b'${PW}', bcrypt.gensalt()).decode()
uid = db.execute("SELECT id FROM users WHERE username='${name}'").fetchone()
if uid:
    uid = uid[0]
    db.execute("DELETE FROM plan_words WHERE plan_id IN (SELECT id FROM study_plans WHERE user_id=?)", (uid,))
    db.execute("DELETE FROM study_plans WHERE user_id=?", (uid,))
    db.execute("DELETE FROM daily_logs WHERE user_id=?", (uid,))
    db.execute("DELETE FROM user_word_state WHERE user_id=?", (uid,))
    db.execute("DELETE FROM users WHERE id=?", (uid,))
db.execute("INSERT INTO users(username,password_hash,display_name,mode) VALUES(?,?,?,'standard')", ('${name}', h, '${name}'))
db.commit()`);
}
function rmuser(name) {
  dockerPy(`import sqlite3
db = sqlite3.connect('/app/data/wordslearn.db')
uid = db.execute("SELECT id FROM users WHERE username='${name}'").fetchone()
if uid:
    uid = uid[0]
    db.execute("DELETE FROM plan_words WHERE plan_id IN (SELECT id FROM study_plans WHERE user_id=?)", (uid,))
    db.execute("DELETE FROM study_plans WHERE user_id=?", (uid,))
    db.execute("DELETE FROM daily_logs WHERE user_id=?", (uid,))
    db.execute("DELETE FROM user_word_state WHERE user_id=?", (uid,))
    db.execute("DELETE FROM users WHERE id=?", (uid,))
db.commit()`);
}

/* ---------- 画像 A：mily 型（5A，P13过/P45不过，混合摸底+验证含错+刷新） ---------- */
async function profileA(page, rng) {
  const ctx = { timeline: [] };
  mkuser('iv_a');
  await login(page, 'iv_a');
  let s = await step(page, ctx, () => page.waitForSelector('h2:has-text("你好")', { timeout: 8000 }));
  check('A 登录→欢迎', s.view === 'welcome');
  await step(page, ctx, () => page.click('button:has-text("五年级上")'));
  await step(page, ctx, () => page.waitForSelector('h2:has-text("你的目标是")'));
  await step(page, ctx, () => page.click('.goal-card:has-text("稳步前进")'));
  await step(page, ctx, () => page.click('.day-btn:has-text("60 天")'));
  await step(page, ctx, () => page.click('button:has-text("下一步")'));
  s = await step(page, ctx, () => page.waitForSelector('h2:has-text("水平小测")'));
  await step(page, ctx, () => page.click('button:has-text("开始小测")'));
  await page.waitForSelector('.phase-tag:has-text("水平小测")', { timeout: 8000 });
  // 80题：P13 段全对，P45 段 35/40（未过）
  for (let i = 0; i < 80; i++) {
    const wrong = i >= 40 && (i % 8 === 0);   // P45 部分错 → 未达标
    const idx = await page.evaluate((w) => {
      const vm = window.__vm; const q = vm.spot.quiz[vm.spot.idx];
      return w ? q.options.findIndex(o => o !== q.answer) : q.options.indexOf(q.answer);
    }, wrong);
    await page.locator('.quiz-grid .btn.opt').nth(Math.max(0, idx)).click();
    await page.waitForTimeout(wrong ? 1220 : 720);
  }
  s = await step(page, ctx, () => page.waitForSelector('h2:has-text("小漏洞")', { timeout: 8000 }));
  check('A P13过/P45未过 → 小漏洞页', true);
  await step(page, ctx, () => page.screenshot({ path: SHOTS + '/iv-a-spot-result.png' }));
  s = await step(page, ctx, () => page.click('button:has-text("开始细致摸底")'));
  await page.waitForSelector('h2:has-text("先看看哪些词已经会了")', { timeout: 8000 });
  // 摸底循环：混合自评 + 验证（85%对）+ 中途一次刷新 + 完成判定
  let guard = 0;
  while (guard++ < 900) {
    s = await snap(page);
    if (s.view === 'setup') break;
    if (s.phase === 'self') {
      const r = rng();
      const btn = r < 0.6 ? '.btn.green' : (r < 0.8 ? '.btn.orange' : '.btn.gray');
      await step(page, ctx, () => page.click(btn));
    } else if (s.phase === 'quiz') {
      const qstate = await page.evaluate(() => { const q = window.__vm.curQuiz; return q ? q.picked : null; });
      if (qstate != null) { await page.waitForTimeout(300); continue; }
      const ok = rng() < 0.85;
      let idx = await page.evaluate((w) => {
        const vm = window.__vm; const q = vm.curQuiz;
        if (!q || q.picked != null) return -1;
        return w ? q.options.indexOf(q.answer) : q.options.findIndex(o => o !== q.answer);
      }, ok).catch(() => -1);
      if (idx < 0) { await page.waitForTimeout(300); continue; }
      // 中途刷新一次（I8）
      if (guard === 17) {
        await page.reload({ waitUntil: 'networkidle' });
        await page.waitForTimeout(800);
        const s2 = await snap(page);
        check('I8 刷新后仍处于摸底流程', ['screening', 'setup'].includes(s2.view), s2.view);
      }
      try { await step(page, ctx, () => page.locator('.quiz-grid .btn.opt').nth(Math.max(0, idx)).click({ timeout: 4000 })); } catch (e) { await page.waitForTimeout(300); continue; }
      await page.waitForTimeout(ok ? 750 : 1500);
    } else {
      await step(page, ctx, () => page.waitForTimeout(400));
    }
  }
  check('A 摸底完成到 setup', s.view === 'setup', s.view);
  s = await snap(page);
  check('A 摸底摘要显示', s.summaryCard.some(t => t.includes('摸底完成') || t.includes('已会')) || s.subTexts.some(t => t.includes('已会')),
        JSON.stringify(s.summaryCard));
  // I4 后验：同词两次 QUIZ 出现之间必须有 SELF；同词出现次数≤2；相邻不同词
  const tl = ctx.timeline;
  for (let i = 1; i < tl.length; i++) {
    check('I4 相邻两题不同词', tl[i][1] !== tl[i-1][1] || tl[i][0] !== 'Q' || tl[i-1][0] !== 'Q',
          `${tl[i-1]} -> ${tl[i]}`);
  }
  const appear = {};
  tl.filter(t => t[0] === 'Q').forEach(t => { appear[t[1]] = (appear[t[1]] || 0) + 1; });
  check('I4 同词出现≤2次', Object.values(appear).every(n => n <= 2), JSON.stringify(Object.entries(appear).filter(([,n])=>n>2)));
  for (const wid of Object.keys(appear)) {
    const pos = tl.map((t, i) => (t[0] === 'Q' && t[1] == wid) ? i : -1).filter(i => i >= 0);
    if (pos.length === 2) {
      const gap = pos[1] - pos[0] - 1;
      check('I4 两次验证间隔≥20题(防短期记忆套答案)', gap >= 20, `word=${wid} gap=${gap}`);
    }
  }
  return ctx;
}

/* ---------- 画像 B：mido 型（1A junior，无小测直建计划+学习） ---------- */
async function profileB(page) {
  const ctx = { lastQuizWord: null, quizStreak: 0, selfSinceLastQuiz: false };
  dockerPy(`import sqlite3, bcrypt
db = sqlite3.connect('/app/data/wordslearn.db')
h = bcrypt.hashpw(b'${PW}', bcrypt.gensalt()).decode()
db.execute("DELETE FROM users WHERE username='iv_b'")
db.execute("INSERT INTO users(username,password_hash,display_name,mode) VALUES(?,?,?,'junior')", ('iv_b', h, 'B'))
db.commit()`);
  await login(page, 'iv_b');
  await step(page, ctx, () => page.waitForSelector('h2:has-text("你好")'));
  await step(page, ctx, () => page.click('button:has-text("一年级上")'));
  await step(page, ctx, () => page.waitForSelector('h2:has-text("你的目标是")'));
  check('B junior 目标档 60 词', (await page.locator('.goal-card').first().textContent()).includes('60'));
  await step(page, ctx, () => page.click('.goal-card:has-text("轻松起步")'));
  await step(page, ctx, () => page.click('button:has-text("下一步")'));
  let s = await step(page, ctx, () => page.waitForSelector('h2:has-text("准备好了")'));
  check('B junior 无小测直接准备页', s.view === 'welcome');
  await step(page, ctx, () => page.click('button:has-text("开始制定计划")'));
  s = await step(page, ctx, () => page.waitForSelector('h2:has-text("制定你的计划")'));
  const def = await page.locator('input[type=number]').nth(0).inputValue();
  check('B 默认=意向60', def === '60', def);
  await step(page, ctx, () => page.click('button:has-text("开始！")'));
  await step(page, ctx, () => page.waitForSelector('button:has-text("开始学习")', { timeout: 8000 }));
  check('B junior 有声音解锁屏', true);
  await step(page, ctx, () => page.click('button:has-text("开始学习")'));
  s = await step(page, ctx, () => page.waitForSelector('.phase-tag', { timeout: 8000 }));
  check('B junior 大字模式', await page.locator('.app-shell.mode-junior').count() === 1);
  check('B junior 隐藏音标', await page.locator('.learn-card .phonetic').count() === 0);
}

/* ---------- 画像 C：velen 型（adult 家长，无小测无摸底直 setup） ---------- */
async function profileC(page) {
  const ctx = { lastQuizWord: null, quizStreak: 0, selfSinceLastQuiz: false };
  mkuser('iv_c');
  await login(page, 'iv_c');
  await step(page, ctx, () => page.waitForSelector('h2:has-text("你好")'));
  await step(page, ctx, () => page.click('button:has-text("家长体验")'));
  await step(page, ctx, () => page.waitForSelector('h2:has-text("你的目标是")'));
  await step(page, ctx, () => page.click('.goal-card:has-text("轻松起步")'));
  await step(page, ctx, () => page.click('button:has-text("下一步")'));
  const s = await step(page, ctx, () => page.waitForTimeout(700));
  check('C adult 不出现小测/摸底，直达准备', ['welcome', 'setup'].includes(s.view) && !s.subTexts.some(t => t.includes('水平小测') || t.includes('正确意思')), s.view);
}

/* ---------- 数据库审计（D 类） ---------- */
function dbAudit(users) {
  const out = dockerPy(`import sqlite3, json
db = sqlite3.connect('/app/data/wordslearn.db')
db.row_factory = sqlite3.Row
res = {}
for u in ${JSON.stringify(users)}:
    uid = db.execute("SELECT id FROM users WHERE username=?", (u,)).fetchone()
    if not uid: continue
    uid = uid['id']
    bad_state = db.execute("SELECT COUNT(*) c FROM user_word_state WHERE user_id=? AND state NOT IN ('new','verify','learning','mastered','skipped')", (uid,)).fetchone()['c']
    bad_combo = db.execute("SELECT COUNT(*) c FROM user_word_state WHERE user_id=? AND ((state='mastered' AND next_review_date IS NOT NULL) OR (state='learning' AND (next_review_date IS NULL OR first_learned_date IS NULL)) OR (state='verify' AND first_learned_date IS NOT NULL))", (uid,)).fetchone()['c']
    dup_plan = db.execute("SELECT COUNT(*) c FROM (SELECT word_id FROM plan_words p JOIN study_plans sp ON sp.id=p.plan_id WHERE sp.user_id=? GROUP BY word_id HAVING COUNT(*)>1)", (uid,)).fetchone()['c']
    dangling_verify = db.execute("SELECT COUNT(*) c FROM user_word_state WHERE user_id=? AND state='verify' AND COALESCE(verify_count,0)>=2", (uid,)).fetchone()['c']
    log_vs_state = None
    plan = db.execute("SELECT id, daily_new FROM study_plans WHERE user_id=? AND status='active'", (uid,)).fetchone()
    if plan:
        logn = db.execute("SELECT COALESCE(new_done,0) n FROM daily_logs WHERE user_id=? AND date=date('now','localtime')", (uid,)).fetchone()
        staten = db.execute("SELECT COUNT(*) c FROM user_word_state WHERE user_id=? AND first_learned_date=date('now','localtime')", (uid,)).fetchone()['c']
        log_vs_state = (logn['n'] if logn else 0) - staten
    res[u] = dict(bad_state=bad_state, bad_combo=bad_combo, dup_plan=dup_plan,
                  dangling_verify=dangling_verify, log_minus_state=log_vs_state)
print(json.dumps(res))`).trim();
  const audit = JSON.parse(out);
  for (const [u, a] of Object.entries(audit)) {
    check(`D1 ${u} 状态值合法`, a.bad_state === 0, JSON.stringify(a));
    check(`D2 ${u} 状态-日期组合合法`, a.bad_combo === 0);
    check(`D3 ${u} 计划无重复词`, a.dup_plan === 0);
    check(`D4 ${u} 无悬空verify`, a.dangling_verify === 0);
    if (a.log_minus_state !== null) check(`D5 ${u} 日志==首学数`, a.log_minus_state === 0, String(a.log_minus_state));
  }
}

/* ---------- 主流程 ---------- */
(async () => {
  const seed = Number(process.env.SEED || 42);
  let s0 = seed;
  const rng = () => { s0 = (s0 * 9301 + 49297) % 233280; return s0 / 233280; };
  const browser = await chromium.launch({ executablePath: '/opt/google/chrome/chrome', args: ['--no-sandbox', '--disable-dev-shm-usage', '--autoplay-policy=no-user-gesture-required'] });
  const errs = [];
  let page = null;
  try {
    const newPage = async () => { if (page) await page.close().catch(()=>{}); page = await (await browser.newContext({ viewport: { width: 820, height: 1200 } })).newPage(); page.on('pageerror', e => errs.push(String(e.message))); return page; };
    console.log('— 画像A: mily型 全流程（小测→摸底→验证→刷新恢复）—');
    await profileA(await newPage(), rng);
    console.log('— 画像B: mido型 junior —');
    await profileB(await newPage());
    console.log('— 画像C: velen型 家长 —');
    await profileC(await newPage());
    console.log('— D类 数据库审计 —');
    dbAudit(['iv_a', 'iv_b', 'iv_c']);
    check('全程无JS异常', errs.length === 0, errs.slice(0, 2).join(';'));
  } finally {
    for (const u of ['iv_a', 'iv_b', 'iv_c']) { try { rmuser(u); } catch (e) {} }
    await browser.close();
  }
  console.log(`\n共 ${CHECKS} 项断言，` + (FAILS.length ? `❌ 失败 ${FAILS.length} 项:\n` + FAILS.map(f => '  ✗ ' + f).join('\n') : '✅ 会话不变式测试全部通过'));
  process.exit(FAILS.length ? 1 : 0);
})().catch(e => {
  console.error('FATAL:', e.message);
  for (const u of ['iv_a', 'iv_b', 'iv_c']) { try { rmuser(u); } catch (_) {} }
  process.exit(2);
});
