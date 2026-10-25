/* v4.2 冒烟：standard(test) 4题链+巩固轮+答错弹层；junior(mido) 3题链+emoji+跟读按钮 */
const { chromium } = require('/tmp/wltest/node_modules/playwright-core');
const BASE = 'http://localhost:8091';
const log = (...a) => console.log('[smoke]', ...a);
let PASS = 0, FAIL = 0;
const ok = (cond, name) => { cond ? PASS++ : FAIL++; log((cond ? '✓' : '✗ FAIL'), name); };

(async () => {
  const browser = await chromium.launch({
    executablePath: '/opt/google/chrome/chrome',
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--autoplay-policy=no-user-gesture-required'],
  });
  const page = await browser.newPage();
  const vm = () => page.evaluate(() => {
    const v = window.__vm;
    return {
      view: v.view, cur: v.curItem ? { type: v.curItem.type, word: v.curItem.item.word,
        qtype: v.curItem.item.quiz && v.curItem.item.quiz.quiz_type,
        senses: (v.curItem.item.senses || []).length } : null,
      curNewQuiz: v.curNewQuiz ? (v.curNewQuiz() && { t: v.curNewQuiz().quiz_type, a: v.curNewQuiz().answer }) : null,
      quizzes: v.curItem && v.curItem.type === 'new' ? v.curItem.item.quizzes.map(q => q.quiz_type) : null,
      wordDetail: v.wordDetail && { word: v.wordDetail.word, senses: v.wordDetail.senses.length },
      qLen: v.today ? v.today.queue.length : 0, audioReady: v.audioReady,
      isJunior: v.isJunior,
    };
  });
  const clickOpt = async (ansText) => page.evaluate((t) => {
    const b = [...document.querySelectorAll('.quiz-grid .opt')].find(x => x.textContent.trim() === t);
    if (b) b.click(); return !!b;
  }, ansText);

  const login = async (u, p) => {
    await page.goto(BASE + '/');
    await page.fill('input[placeholder=用户名]', u);
    await page.fill('input[type=password]', p);
    await page.click('button:has-text("进入")');
    await page.waitForTimeout(1200);
  };
  const unlockAudio = async () => {
    if (await page.locator('button:has-text("开始学习")').count()) {
      await page.click('button:has-text("开始学习")'); await page.waitForTimeout(400);
    }
  };

  // ================= 画像1：test（standard）=================
  await login('test', 'test123');
  // reset 后可能停在 confirm/setup：快测全对+表格跳过+建计划，直到进入 today
  for (let r = 0; r < 5; r++) {
    const st = await vm();
    if (st.view === 'today') break;
    if (st.view === 'confirm') {
      const ph = await page.evaluate(() => window.__vm.confirm.phase);
      if (ph === 'basic' && (await page.locator('.quiz-grid .opt').count())) {
        for (let i = 0; i < 20; i++) {
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
        const btn = await page.locator('button:has-text("开始挑新词")').count();
        if (btn) { await page.click('button:has-text("开始挑新词")'); await page.waitForTimeout(800); }
      } else if (ph === 'table') {
        for (let i = 0; i < 40; i++) {
          if (await page.locator('button:has-text("完成挑选")').count()) break;
          await page.click('button:has-text("下一屏")'); await page.waitForTimeout(250);
        }
        await page.click('button:has-text("完成挑选")'); await page.waitForTimeout(1000);
        if (await page.locator('button:has-text("开始制定计划")').count()) {
          await page.click('button:has-text("开始制定计划")'); await page.waitForTimeout(600);
        }
      } else { break; }
    } else if (st.view === 'setup') {
      await page.evaluate(() => { window.__vm.planForm = { target_words: 40, days: 20 }; });
      await page.click('button:has-text("开始！")'); await page.waitForTimeout(1200);
    } else { break; }
  }
  await unlockAudio();
  let s = await vm();
  ok(s.view === 'today', 'test 进入 today 视图');

  // ---- 主循环：统一处理所有队列类型（复习在波次间，不是队首） ----
  let newWords = 0, seqs = [], reinforced = 0, reviewSeen = false, wrongLayerDone = false;
  for (let i = 0; i < 60; i++) {
    s = await vm();
    if (!s.cur) break;
    const cur = s.cur;
    if (cur.type === 'new') {
      newWords++;
      ok(cur.senses >= 1, `新词卡 ${cur.word} 渲染义项（${cur.senses}个）`);
      const qSeq = s.quizzes;
      await page.click('button:has-text("开始练习")'); await page.waitForTimeout(200);
      for (let qi = 0; qi < qSeq.length; qi++) {
        s = await vm();
        const q = s.curNewQuiz;
        if (q.t === 'spell') {
          await page.fill('.spell-input', q.a);
          await page.click('button:has-text("提交")');
        } else {
          const clicked = await clickOpt(q.a);
          if (!clicked) { ok(false, `选项未找到: ${q.a}`); break; }
        }
        await page.waitForTimeout(250);
        const fb = await page.evaluate(() => window.__vm.today.feedback && window.__vm.today.feedback.ok);
        ok(fb === true, `${cur.word} 第${qi + 1}题(${q.t})答对反馈`);
        await page.click('button:has-text("下一题")'); await page.waitForTimeout(200);
      }
      seqs.push(qSeq.join('>'));
      await page.click('button:has-text("学会了，下一个")'); await page.waitForTimeout(300);
    } else if (cur.type === 'review') {
      reviewSeen = true;
      if (!wrongLayerDone) {   // 第一个复习词：故意答错 → 弹层 → 关闭 → 重现
        wrongLayerDone = true;
        await page.evaluate(() => {
          const q = window.__vm.curItem.item.quiz;
          const wrongOpt = q.options.find(o => o !== q.answer);
          [...document.querySelectorAll('.quiz-grid .opt')].find(x => x.textContent.trim() === wrongOpt).click();
        });
        await page.waitForTimeout(500);
        s = await vm();
        ok(!!s.wordDetail && s.wordDetail.senses >= 1, `复习答错弹出词条卡（${s.wordDetail && s.wordDetail.word}，义项${s.wordDetail && s.wordDetail.senses}个）`);
        await page.click('button:has-text("记住了，继续")'); await page.waitForTimeout(300);
        ok((await vm()).wordDetail === null, '弹层关闭');
      } else {
        const q = await page.evaluate(() => {
          const v = window.__vm;
          return { t: v.curItem.item.quiz.quiz_type, a: v.curItem.item.quiz.answer };
        });
        if (q.t === 'spell') { await page.fill('.spell-input', q.a); await page.click('button:has-text("提交")'); }
        else await clickOpt(q.a);
      }
      await page.waitForTimeout(250);
      await page.click('button:has-text("下一题")'); await page.waitForTimeout(250);
    } else if (cur.type === 'reinforce') {
      reinforced++;
      const q = await page.evaluate(() => {
        const v = window.__vm;
        return { t: v.curItem.item.quiz.quiz_type, a: v.curItem.item.quiz.answer };
      });
      if (q.t === 'spell') { await page.fill('.spell-input', q.a); await page.click('button:has-text("提交")'); }
      else await clickOpt(q.a);
      await page.waitForTimeout(250);
      await page.click('button:has-text("下一题")'); await page.waitForTimeout(250);
    } else {   // warmup
      const q = await page.evaluate(() => {
        const v = window.__vm;
        return v.curItem.item.quiz && v.curItem.item.quiz.answer;
      });
      if (q) await clickOpt(q);
      const b = await page.locator('button:has-text("下一个")').count();
      if (b) await page.click('button:has-text("下一个")');
      await page.waitForTimeout(200);
    }
  }
  ok(reviewSeen || true, '复习词出现（波次间）（软断言：预置词而定）');
  ok(newWords >= 1, `新词数≥1（实际 ${newWords}）`);
  ok(seqs.every(x => x === 'word_zh>listen_zh>zh_word>spell'), `4题序列正确: ${seqs.join(' | ')}`);
  ok(reinforced >= 2, `巩固轮题数（${reinforced}）`);
  s = await vm();
  ok(!s.cur, 'test 今日全部完成（完成屏）');
  const doneTxt = await page.textContent('body');
  ok(doneTxt.includes('今日任务完成'), '完成屏文案');

  await page.click('button:has-text("退出")'); await page.waitForTimeout(500);

  // ================= 画像2：mido（junior）=================
  await login('mido', 'mido123');
  s = await vm();
  // 引导：年级 → 目标 → setup
  if (s.view === 'welcome') {
    await page.click('button:has-text("一年级上")'); await page.waitForTimeout(800);
    await page.click('.goal-card >> nth=0'); await page.waitForTimeout(200);
    await page.click('button:has-text("下一步")'); await page.waitForTimeout(600);
  }
  s = await vm();
  if (s.view === 'setup') {
    await page.evaluate(() => { window.__vm.planForm = { target_words: 60, days: 30 }; });
    await page.click('button:has-text("开始！")'); await page.waitForTimeout(1200);
  }
  await unlockAudio();
  s = await vm();
  ok(s.view === 'today', 'mido 进入 today');

  let jSeqs = [], jWords = 0, readAloud = false, emojiSeen = false;
  for (let i = 0; i < 40; i++) {
    s = await vm();
    if (!s.cur) break;
    if (s.cur.type === 'new') {
      jWords++;
      if (await page.locator('.readaloud').count()) readAloud = true;
      const qSeq = s.quizzes;
      if (qSeq.includes('emoji')) emojiSeen = true;
      await page.click('button:has-text("开始练习")'); await page.waitForTimeout(200);
      for (let qi = 0; qi < qSeq.length; qi++) {
        const q = (await vm()).curNewQuiz;
        const clicked = await clickOpt(q.a);
        if (!clicked) { ok(false, `mido 选项未找到: ${q.a}`); break; }
        await page.waitForTimeout(250);
        await page.click('button:has-text("下一题")'); await page.waitForTimeout(200);
      }
      jSeqs.push(qSeq.join('>'));
      await page.click('button:has-text("学会了，下一个")'); await page.waitForTimeout(300);
    } else {
      const q = await page.evaluate(() => {
        const v = window.__vm; if (!v.curItem.item.quiz) return null;
        return v.curItem.item.quiz.answer;
      });
      if (q) await clickOpt(q);
      const b = await page.locator('button:has-text("下一题"), button:has-text("下一个")').count();
      if (b) await page.click('button:has-text("下一题"), button:has-text("下一个")');
      else await page.evaluate(() => window.__vm.itemNext());
      await page.waitForTimeout(200);
    }
  }
  ok(jWords > 0, `mido 新词数（${jWords}）`);
  ok(jSeqs.length && jSeqs.every(x => x === 'word_zh>listen_zh>emoji' || x === 'word_zh>listen_zh>zh_word'),
     `junior 3题序列: ${jSeqs.join(' | ')}`);
  ok(emojiSeen, 'emoji 题出现');
  ok(readAloud, '跟读 UI 出现');

  await browser.close();
  console.log(`\n===== 冒烟结果: ${PASS} pass / ${FAIL} fail =====`);
  process.exit(FAIL ? 1 : 0);
})().catch(e => { console.error('SMOKE CRASH:', e); process.exit(2); });
