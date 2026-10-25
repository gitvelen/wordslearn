/* v2.1 冒烟：全部呈现→复习沉淀→分关测验（词序打乱·关末错词重测·答对自动过·答错弹卡） */
const { chromium } = require('/tmp/wltest/node_modules/playwright-core');
const BASE = process.env.WL_BASE || 'http://localhost:8091';
const log = (...a) => console.log('[smoke]', ...a);
let PASS = 0, FAIL = 0;
const ok = (c, n) => { c ? PASS++ : FAIL++; log((c ? '✓' : '✗ FAIL'), n); };

(async () => {
  const browser = await chromium.launch({
    executablePath: '/opt/google/chrome/chrome',
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--autoplay-policy=no-user-gesture-required'],
  });
  const page = await browser.newPage();
  const vm = () => page.evaluate(() => {
    const v = window.__vm;
    return {
      view: v.view,
      cur: v.curItem ? { type: v.curItem.type, word: v.curItem.item && v.curItem.item.word,
        level: v.curItem._level, qi: v.curItem._qi, qn: v.curItem._qn, retry: !!v.curItem._retry } : null,
      qtype: v.curItem && v.curItem.item && v.curItem.item.quiz && v.curItem.item.quiz.quiz_type,
      qAnswer: v.curItem && v.curItem.item && v.curItem.item.quiz && v.curItem.item.quiz.answer,
      opts: v.curItem && v.curItem.item && v.curItem.item.quiz && v.curItem.item.quiz.options,
      wordDetail: v.wordDetail && { mode: v.detailMode, locked: v.detailLocked },
      detailLocked: v.detailLocked,
      newLen: v.today ? v.today.new.length : 0,
      doneNew: v.today && v.today.doneNew, doneQuiz: v.today && v.today.doneQuiz,
      gateWrong: v.today && v.today.gateWrong,
    };
  });
  const clickOpt = (t) => page.evaluate((t2) => {
    const b = [...document.querySelectorAll('.quiz-grid .opt')].find(x => x.textContent.trim() === t2);
    if (b) b.click(); return !!b;
  }, t);
  const answer = async (correct) => {
    const s = await vm();
    const spellOk = async (txt) => {   // 拼写格：经 vm 钩子填充（格子可能因自动推进刚渲染，稍等）
      await page.waitForTimeout(200);
      await page.evaluate((t) => { window.__vm.spellType(t); }, txt);
      await page.evaluate(() => { const b = [...document.querySelectorAll('button')].find(x => x.textContent.includes('提交')); if (b && !b.disabled) b.click(); });
    };
    if (correct) {
      const c = await clickOpt(s.qAnswer);
      if (!c && s.qtype === 'spell') await spellOk(s.qAnswer);
    } else {
      const wrong = (s.opts || []).find(o => o !== s.qAnswer);
      if (wrong) await clickOpt(wrong);
      else if (s.qtype === 'spell') await spellOk('x');
    }
  };
  const login = async (u, pw) => {
    await page.goto(BASE + '/');
    await page.fill('input[placeholder=用户名]', u);
    await page.fill('input[type=password]', pw);
    await page.click('button:has-text("进入")');
    await page.waitForTimeout(1200);
  };
  const ensurePlan = async () => {   // confirm/setup → today
    for (let r = 0; r < 5; r++) {
      const st = await vm();
      if (st.view === 'today') break;
      if (st.view === 'confirm') {
        const ph = await page.evaluate(() => window.__vm.confirm.phase);
        if (ph === 'basic' && (await page.locator('.quiz-grid .opt').count())) {
          for (let i = 0; i < 25; i++) {
            const done = await page.evaluate(() => {
              const v = window.__vm;
              if (v.confirm.phase !== 'basic') return true;
              const q = v.curBasicQuiz(); if (!q) return true;
              if (v.confirm.basicPicked == null && !v.locking) {
                const b = [...document.querySelectorAll('.quiz-grid .opt')].find(x => x.textContent.trim() === q.answer);
                if (b) b.click();
              }
              return false;
            });
            if (done) break;
            await page.waitForTimeout(350);
          }
          await page.waitForTimeout(1200);
          if (await page.locator('button:has-text("开始挑新词")').count()) {
            await page.click('button:has-text("开始挑新词")'); await page.waitForTimeout(800);
          }
        } else if (ph === 'table') {
          for (let i = 0; i < 40; i++) {
            if (await page.locator('button:has-text("完成挑选")').count()) break;
            await page.click('button:has-text("下一屏")'); await page.waitForTimeout(250);
          }
          await page.click('button:has-text("完成挑选")'); await page.waitForTimeout(1000);
          if (await page.locator('button:has-text("开始制定计划")').count()) {
            await page.click('button:has-text("开始制定计划")'); await page.waitForTimeout(600);
          }
        } else break;
      } else if (st.view === 'setup') {
        await page.evaluate(() => { window.__vm.planForm = { target_words: 40, days: 20 }; });
        await page.click('button:has-text("开始！")'); await page.waitForTimeout(1200);
      } else break;
    }
    if (await page.locator('button:has-text("开始学习")').count()) {
      await page.click('button:has-text("开始学习")'); await page.waitForTimeout(400);
    }
  };

  // ================= test（standard 五关）=================
  let reportReq = 0;
  page.on('request', r => { if (r.url().includes('/api/report/daily')) reportReq++; });
  const waitCardUnlock = async () => {   // 卡片 2.5s 注视期
    for (let i = 0; i < 12; i++) {
      if (!(await page.evaluate(() => window.__vm.cardLocked))) return;
      await page.waitForTimeout(400);
    }
  };
  await login('test', 'test123');
  await ensurePlan();
  let s = await vm();
  ok(s.view === 'today' && s.cur, 'test 进入 today 有任务');

  let cards = 0, reviews = 0, quizLevels = [], gates = [], wrongPopupDone = false, quizWrongDone = false;
  let firstGatePassed = false, mutedChecked = false, emptySpellChecked = false;
  let cardLockSeen = false, advGuardChecked = false;
  for (let step = 0; step < 400; step++) {
    s = await vm();
    if (!s.cur) break;
    const cur = s.cur;
    if (cur.type === 'newcard') {
      cards++;
      if (!cardLockSeen) {
        cardLockSeen = true;
        ok(await page.evaluate(() => window.__vm.cardLocked) === true, '新词卡片进卡注视期锁定（防秒过）');
      }
      await waitCardUnlock();
      const btn = await page.locator('button:has-text("记住了，下一个"), button:has-text("都记住了")').count();
      if (btn) await page.click('button:has-text("记住了，下一个"), button:has-text("都记住了")');
      await page.waitForTimeout(250);
    } else if (cur.type === 'review') {
      reviews++;
      if (!wrongPopupDone) {           // 预置复习词首答错 → 弹层 → 1s 锁 → 继续自动推进
        wrongPopupDone = true;
        await answer(false); await page.waitForTimeout(600);
        s = await vm();
        ok(s.wordDetail && s.wordDetail.mode === 'wrong', '复习答错弹词条卡');
        ok(s.detailLocked === true, '弹层按钮 1s 注视期锁定');
        await page.waitForTimeout(1100);
        await page.click('button:has-text("记住了，继续")'); await page.waitForTimeout(800);
      } else {
        await answer(true); await page.waitForTimeout(1300);   // 答对自动过
      }
    } else if (cur.type === 'quiz') {
      quizLevels.push(cur.level);
      // 未作答硬闸：直接调 advance 不得推进
      if (!advGuardChecked) {
        advGuardChecked = true;
        const before = await page.evaluate(() => window.__vm.today.qIdx);
        await page.evaluate(() => window.__vm.advance());
        const after = await page.evaluate(() => window.__vm.today.qIdx);
        ok(before === after, '未作答调用 advance 不推进（硬闸生效）');
      }
      // zh_word/cloze 不显示 🔊（读出目标词=报答案）
      if ((s.qtype === 'zh_word' || s.qtype === 'cloze') && !mutedChecked) {
        mutedChecked = true;
        const n = await page.locator('.quiz-card .btn.round').count();
        ok(n === 0, `${s.qtype} 题无 🔊 按钮（防听音猜答案）`);
      }
      // 空拼写提交不判错
      if (s.qtype === 'spell' && !emptySpellChecked) {
        emptySpellChecked = true;
        await page.evaluate(() => { window.__vm.spellSubmit(); });
        const fb = await page.evaluate(() => window.__vm.today.feedback);
        ok(fb === null, '空拼写提交不判错（无反馈不推进）');
        await page.waitForTimeout(300);
      }
      // 第 1 关第 1 题故意答错（验证关末重测闭环），其余全对
      if (cur.level === 1 && !quizWrongDone) {
        quizWrongDone = true;
        await answer(false); await page.waitForTimeout(600);
        await page.click('button:has-text("记住了，继续")'); await page.waitForTimeout(800);
      } else {
        await answer(true); await page.waitForTimeout(1400);
      }
    } else if (cur.type === 'gate') {
      gates.push(cur.level);
      const gateWrong = await page.evaluate((lv) => window.__vm.gateWrongWords(lv).length, cur.level);
      if (cur.level === 1 && !firstGatePassed) {
        firstGatePassed = true;
        ok(gateWrong === 1, `第1关结算含 1 个错词（实际 ${gateWrong}）`);
        await page.click('button:has-text("把错的再来一遍")'); await page.waitForTimeout(500);
        // 补测答对 → gate 全对 → 下一关
        // （补测题在循环里处理，会再遇到 gate=1）
      } else {
        const hasBtn = await page.locator('button:has-text("把错的再来一遍")').count();
        if (hasBtn) { await page.click('button:has-text("把错的再来一遍")'); await page.waitForTimeout(400); }
        else { await page.click('button:has-text("下一关")'); await page.waitForTimeout(400); }
      }
    } else {   // warmup
      await answer(true); await page.waitForTimeout(1300);
      if (await page.locator('button:has-text("继续")').count()) {
        await page.click('button:has-text("继续")'); await page.waitForTimeout(300);
      }
    }
  }
  ok(cards === (await vm()).newLen || cards > 0, `卡片连播 ${cards} 张`);
  ok(reviews >= 1, `复习题 ${reviews} 个（沉淀间隔位）`);
  const uniqLevels = [...new Set(quizLevels)];
  ok(uniqLevels.length === 5, `五关齐全: ${uniqLevels.join(',')}`);
  ok(quizLevels.every((l, i) => i === 0 || l >= quizLevels[i - 1]), '关卡顺序非降（含补测回流）');
  ok(gates.length >= 5, `关末结算 ${gates.length} 次（含重测后再结算）`);
  // 第 1 关补测闭环：quizLevels 中 1 出现次数 > 第 1 关题数？直接断言 doneQuiz > 5*newLen 的下限不严格——断言 gate1 出现≥2 次
  ok(gates.filter(g => g === 1).length >= 2, '第 1 关错词重测后再次结算');
  s = await vm();
  ok(!s.cur, 'test 完成屏');
  const bodyTxt = await page.textContent('body');
  ok(bodyTxt.includes('今日任务完成'), '完成屏文案');
  ok(bodyTxt.includes('关') && bodyTxt.includes('🏅'), '完成屏含闯关统计');
  await page.waitForTimeout(500);
  ok(reportReq >= 1, `完成触发学习日报 POST（${reportReq} 次）`);
  await page.waitForTimeout(600);
  const sc = await page.evaluate(() => window.__vm.scoreState);
  ok(sc && sc.today != null && sc.today > 0 && sc.today <= 100, `完成屏积分已结算展示（${sc && sc.today} 分）`);
  await page.click('button:has-text("退出")'); await page.waitForTimeout(400);

  // ================= junior（三关 + 跟读）——用临时账号 jrsmoke，勿动生产 mido ==================
  await login(process.env.WL_JR_USER || 'jrsmoke', 'test123');
  await ensurePlan();
  let jLevels = new Set(), jCards = 0, readAloudSeen = false;
  for (let step = 0; step < 300; step++) {
    s = await vm();
    if (!s.cur) break;
    if (s.cur.type === 'newcard') {
      jCards++;
      if (await page.locator('.readaloud').count()) readAloudSeen = true;
      await waitCardUnlock();
      await page.click('button:has-text("记住了，下一个"), button:has-text("都记住了")');
      await page.waitForTimeout(250);
    } else if (s.cur.type === 'quiz') {
      jLevels.add(s.cur.level);
      await answer(true); await page.waitForTimeout(1400);
    } else if (s.cur.type === 'gate') {
      const hasBtn = await page.locator('button:has-text("把错的再来一遍")').count();
      if (hasBtn) { await page.click('button:has-text("把错的再来一遍")'); await page.waitForTimeout(400); }
      else { await page.click('button:has-text("下一关")'); await page.waitForTimeout(400); }
    } else {
      await answer(true); await page.waitForTimeout(1300);
      if (await page.locator('button:has-text("继续")').count()) {
        await page.click('button:has-text("继续")'); await page.waitForTimeout(300);
      }
    }
  }
  ok(jCards > 0, `mido 卡片 ${jCards} 张`);
  ok(jLevels.size === 3, `junior 三关（实际 ${jLevels.size} 关: ${[...jLevels].join(',')}）`);
  ok(readAloudSeen, '跟读区在卡片上');
  await browser.close();
  console.log(`\n===== v2.1 冒烟: ${PASS} pass / ${FAIL} fail =====`);
  process.exit(FAIL ? 1 : 0);
})().catch(e => { console.error('CRASH:', e); process.exit(2); });
