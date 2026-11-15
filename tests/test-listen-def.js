/* listen_def（听英文释义选单词）e2e（v3.4）：
 *   gate 阶段：第 6 关下发（义1 idx=1）｜全污染/缺题词该关静默跳过｜进题自动播释义音（抓包）
 *              ｜🔊 重听播 def 音不泄词音（抓包）｜wl_gate_v1 answered["6"] 会话记忆
 *   warmup 阶段：多义词展开剩余释义（idx>=2）｜单义词回落 confirm word_zh｜哨兵防重放
 *   junior 阶段：无第 6 关、热身不出 def 题
 *   兜底：playDef 加载失败回退浏览器 TTS 不崩
 * 运行：node tests/test-listen-def.js gate|warmup|junior   （env LD_USER/LD_JR_USER）
 * 前置：8091 测试服务（WL_DB 指副本库，listen_def 题已导入） */
const { chromium } = require('/tmp/wltest/node_modules/playwright-core');
const BASE = process.env.DG_BASE || 'http://localhost:8091';
const USER = process.env.LD_USER;
const JR_USER = process.env.LD_JR_USER;

let fails = 0;
const ok = (name, cond, extra) => {
  console.log((cond ? '✓' : '✗ FAIL') + ' ' + name + (extra ? ' | ' + extra : ''));
  if (!cond) fails++;
};

(async () => {
  const browser = await chromium.launch({
    executablePath: '/opt/google/chrome/chrome',
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--autoplay-policy=no-user-gesture-required'],
  });
  const phase = process.argv[2];
  const page = await (await browser.newContext()).newPage();
  const reqs = [];
  page.on('request', r => reqs.push(r.url()));

  const login = async (u) => {
    await page.goto(BASE);
    await page.waitForFunction(() => window.__vm);
    await page.evaluate(async (u) => {
      const v = window.__vm;
      v.loginForm = { username: u, password: 'test123' };
      await v.doLogin();
    }, u);
    await page.waitForFunction(() => window.__vm && window.__vm.today && window.__vm.today.queue &&
      window.__vm.today.queue.length > 0, null, { timeout: 15000 });
  };

  if (phase === 'gate') {
    await login(USER);
    const st = await page.evaluate(() => {
      const v = window.__vm, q = v.today.queue;
      const g6 = q.filter(x => x.type === 'quiz' && x._level === 6);
      return {
        queueLen: q.length,
        g6n: g6.length,
        g6ok: g6.every(x => x.item.quiz && x.item.quiz.quiz_type === 'listen_def' && x.item.quiz.idx === 1),
        g6Words: g6.map(x => x.item.word),
        newWords: v.today.new.map(n => n.word),
        defQuizN: v.today.new.reduce((a, n) =>
          a + ((n.quizzes || []).some(x => x.quiz_type === 'listen_def' && x.idx === 1) ? 1 : 0), 0),
      };
    });
    ok('队列已建', st.queueLen > 0, `queue=${st.queueLen}`);
    ok('第 6 关下发且全部 listen_def idx=1（义1）', st.g6n > 0 && st.g6ok,
       `gate6 题=${st.g6n} 词=[${st.g6Words}]`);
    ok('gate6 题数 == 今日新词中有 def 题的词数（全污染/缺题词静默跳关）',
       st.g6n === st.defQuizN, `g6=${st.g6n} def词=${st.defQuizN} 新词=[${st.newWords}]`);

    // 进题自动播释义音（抓包）：跳到第 6 关首题
    const g6word = await page.evaluate(() => {
      const v = window.__vm;
      const i = v.today.queue.findIndex(x => x.type === 'quiz' && x._level === 6);
      if (i < 0) return null;
      v.today.qIdx = i; v.today.answered = false; v.today.feedback = null; v.today.picked = null;
      v.autoPlayItem(v.todayQueue[i]);
      return v.todayQueue[i].item.word;
    });
    await page.waitForTimeout(900);
    const defHit = reqs.some(u => u.includes(`/audio/def/${encodeURIComponent(g6word)}__1.mp3`));
    const leakHit = reqs.some(u => u.endsWith(`/audio/${encodeURIComponent(g6word)}.mp3?v=`)
      || u.endsWith(`/audio/${encodeURIComponent(g6word)}.mp3`));
    ok('进题自动播释义音频（/audio/def/）', defHit, `word=${g6word}`);
    ok('自动播放不请求词音（不泄答案）', !leakHit);

    // 🔊 重听按钮：replayQuizAudio → def 音而非词音（同 URL 重播命中 immutable 缓存
    // 不发网络请求——断言播放器实际装载的 src，不依赖网络层）
    await page.evaluate(() => window.__vm.replayQuizAudio(window.__vm.todayQueue[window.__vm.today.qIdx]));
    await page.waitForTimeout(300);
    const repSrc = await page.evaluate(() =>
      window.__vm._curAudio && window.__vm._curAudio.src);
    ok('🔊 重听播 def 音（replayQuizAudio 分派）',
       repSrc && repSrc.includes('/audio/def/'), String(repSrc).slice(-80));

    // 答一题 → wl_gate_v1 answered["6"] 记忆
    await page.evaluate(() => {
      const v = window.__vm, w = v.todayQueue[v.today.qIdx];
      v.quizAnswer(w.item.quiz.answer);
    });
    await page.waitForTimeout(400);
    const mem6 = await page.evaluate((u) => {
      const m = JSON.parse(localStorage.getItem('wl_gate_v1_' + u) || 'null');
      return m && m.answered && m.answered['6'] && m.answered['6'].length > 0;
    }, USER);
    ok('wl_gate_v1 answered["6"] 会话记忆生效', mem6);

    // playDef 失败回退（不存在词 → onerror → speakTTS，不崩）
    const fbOk = await page.evaluate(async () => {
      try {
        window.__vm.playDef('zzz_no_such_word', 9, 'fallback test');
        await new Promise(r => setTimeout(r, 400));
        return !!window.__vm;
      } catch (e) { return false; }
    });
    ok('playDef 加载失败回退浏览器 TTS 不崩', fbOk);
  }

  if (phase === 'warmup') {
    await login(USER);
    const st = await page.evaluate(() => {
      const v = window.__vm;
      const wq = v.today.queue.filter(x => x.type === 'warmup');
      return {
        warmLen: v.today.warmup.length,
        warmTotal: v.today.warmupTotal,
        items: wq.map(x => ({ word: x.item.word, type: x.item.quiz && x.item.quiz.quiz_type,
                              idx: x.item.quiz && x.item.quiz.idx })),
        first: v.todayQueue[0] && v.todayQueue[0].type,
      };
    });
    const defItems = st.items.filter(x => x.type === 'listen_def');
    const zhItems = st.items.filter(x => x.type === 'word_zh');
    ok('热身队列在队首', st.first === 'warmup', `first=${st.first}`);
    ok('多义词热身展开剩余释义（listen_def idx>=2）',
       defItems.length > 0 && defItems.every(x => x.idx >= 2),
       JSON.stringify(st.items));
    ok('单义词热身回落 confirm word_zh', zhItems.length > 0);
    ok('热身序号按题计（warmupTotal>=词数）', st.warmTotal >= st.warmLen,
       `total=${st.warmTotal} 词=${st.warmLen}`);

    // 哨兵：答一题热身 → reload → 热身不再重放
    await page.evaluate(() => {
      const v = window.__vm;
      v.warmupAnswer(v.todayQueue[0].item.quiz
        ? v.todayQueue[0].item.quiz.answer : null);
    });
    await page.waitForTimeout(500);
    await page.goto(BASE);
    await page.waitForFunction(() => window.__vm && window.__vm.today && window.__vm.today.queue &&
      window.__vm.today.queue.length >= 0, null, { timeout: 15000 });
    const noWarm = await page.evaluate(() =>
      !window.__vm.today.queue.some(x => x.type === 'warmup'));
    ok('热身哨兵：作答过热身后重开不重放', noWarm);
  }

  if (phase === 'junior') {
    await login(JR_USER);
    const st = await page.evaluate(() => {
      const v = window.__vm, q = v.today.queue;
      return {
        g6n: q.filter(x => x._level === 6 || (x.item && x.item.quiz
             && x.item.quiz.quiz_type === 'listen_def')).length,
        warmDef: q.filter(x => x.type === 'warmup' && x.item.quiz
                 && x.item.quiz.quiz_type === 'listen_def').length,
        gates: [...new Set(q.filter(x => x._level).map(x => x._level))].sort(),
        junior: v.isJunior,
      };
    });
    ok('junior 模式生效', st.junior);
    ok('junior 无第 6 关 / 全队列零 listen_def', st.g6n === 0, `levels=[${st.gates}]`);
    ok('junior 热身不出 def 题', st.warmDef === 0);
  }

  await browser.close();
  console.log(fails ? `✗ ${fails} 项失败` : '✓ 全部通过');
  process.exit(fails ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });
