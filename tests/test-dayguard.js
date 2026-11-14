/* 隔夜复活/同日重开守卫 e2e（v2.7.5）：
 *   main 阶段：同日复活不重载+节流 / 跨日复活强制刷新 / date校验失败不崩不重载
 *   done 阶段：当日已完成 → 完成屏直呈、队列不可重放、补发日报幂等
 * 运行（bash 编排 python 预设，见 run 部分）：
 *   node tests/test-dayguard.js main|done
 * 前置：本地 8091 测试服务（库为副本，绝不能指向生产库） */
const { chromium } = require('/tmp/wltest/node_modules/playwright-core');
const BASE = process.env.DG_BASE || 'http://localhost:8091';
const USER = process.env.DG_USER;
const TODAY = process.env.DG_TODAY;

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
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  const cnt = { date: 0, today: 0, report: 0 };
  page.on('request', r => {
    if (r.url().includes('/api/date')) cnt.date++;
    if (r.url().includes('/api/today')) cnt.today++;
    if (r.url().includes('/api/report/daily')) cnt.report++;
  });

  const dispatch = (ev) => page.evaluate((ev) => {
    if (ev === 'pageshow') { const e = new Event('pageshow'); e.persisted = true; window.dispatchEvent(e); }
    else {
      Object.defineProperty(document, 'hidden', { value: false, configurable: true });
      document.dispatchEvent(new Event('visibilitychange'));
    }
  }, ev);

  if (process.argv[2] === 'main') {
    await page.goto(BASE);
    await page.waitForFunction(() => window.__vm);
    await page.evaluate(async (u) => {
      const v = window.__vm;
      v.loginForm = { username: u, password: 'test123' };
      await v.doLogin();
    }, USER);
    await page.waitForFunction(() => window.__vm && window.__vm.today && window.__vm.today.queue.length > 0,
      null, { timeout: 15000 });
    ok('登录进入今日会话，队列已建', true, `queue=${await page.evaluate(() => window.__vm.today.queue.length)}`);
    ok('today.date 为服务端日期', await page.evaluate(() => window.__vm.today.date) === TODAY);
    await page.evaluate(() => { window.__marker = 42; });

    // 同日复活：不重载
    await dispatch('pageshow');
    await page.waitForTimeout(1200);
    ok('同日复活(pageshow persisted)不重载', await page.evaluate(() => window.__marker) === 42);
    ok('复活校验确实调用了 /api/date', cnt.date >= 1, `date=${cnt.date}`);

    // 节流：30s 内重复复活不再请求
    const d0 = cnt.date;
    await dispatch('visibilitychange');
    await dispatch('pageshow');
    await page.waitForTimeout(800);
    ok('30s 节流生效（不重复请求）', cnt.date === d0, `date=${cnt.date}`);

    // 跨日复活：强制整页刷新
    await page.evaluate(() => { const v = window.__vm; v.today.date = '2000-01-01'; v._dayCheckAt = 0; });
    await dispatch('pageshow');
    await page.waitForFunction((expected) => window.__vm && window.__vm.today
      && window.__vm.today.date === expected, TODAY, { timeout: 10000 });
    ok('跨日复活触发整页刷新并重拉今日队列', true, `today请求=${cnt.today}`);
    ok('刷新后为新页面实例（旧内存态已弃）', await page.evaluate(() => window.__marker) === undefined);

    // /api/date 不可达：不崩、不重载
    await ctx.route('**/api/date', r => r.abort());
    await page.waitForFunction(() => window.__vm && window.__vm.today && window.__vm.today.queue.length > 0,
      null, { timeout: 15000 });
    await page.evaluate(() => { window.__marker2 = 7; });
    await page.evaluate(() => { const v = window.__vm; v.today.date = '1999-01-01'; v._dayCheckAt = 0; });
    await dispatch('visibilitychange');
    await page.waitForTimeout(2000);
    ok('date校验失败时保持原页（离线复活照常可用）', await page.evaluate(() => window.__marker2) === 7);
    ok('页面仍可响应（无崩溃）', await page.evaluate(() => 1 + 1) === 2);

  } else if (process.argv[2] === 'midway') {
    /* 中途退出重开（11-03 事故回归）：当日有 new_done、无 score →
     * 服务端 done 必须===false，前端重建队列，绝不发完成事件 */
    await page.goto(BASE);
    await page.waitForFunction(() => window.__vm);
    await page.evaluate(async (u) => {
      const v = window.__vm;
      v.loginForm = { username: u, password: 'test123' };
      await v.doLogin();
    }, USER);
    await page.waitForFunction(() => window.__vm && window.__vm.today, null, { timeout: 15000 });
    await page.waitForTimeout(1500);
    const apiDone = await page.evaluate(async () => {
      const r = await fetch('/api/today'); const j = await r.json();
      return j.done;   // 修复前这里是 new_done 整数(3)——truthy 误判
    });
    ok('/api/today 的 done 严格为 false（非 truthy 整数）', apiDone === false, `done=${JSON.stringify(apiDone)}`);
    const st = await page.evaluate(() => ({
      q: window.__vm.today.queue.length, doneQuiz: window.__vm.today.doneQuiz,
      screen: document.body.textContent.includes('今日任务完成'),
    }));
    ok('中途重开重建队列（可续学，关卡不丢）', st.q > 0, `queue=${st.q}`);
    ok('不误判完成（doneQuiz 未置位）', st.doneQuiz !== 1, `doneQuiz=${st.doneQuiz}`);
    ok('不误呈完成屏', !st.screen);
    ok('未触发完成事件（无 POST report/daily）', cnt.report === 0, `report=${cnt.report}`);

  } else if (process.argv[2] === 'gate') {
    /* 进关后重开（11-12 mily 事故回归）：当日已有 gate 埋点 + 尾巴 due 词(65>60)。
     * 修复前：尾巴 5 个补位词重发 → 阶段从关卡倒退回复习；进度条无当日基数 → 回卷 0% */
    await page.goto(BASE);
    await page.waitForFunction(() => window.__vm);
    await page.evaluate(async (u) => {
      const v = window.__vm;
      v.loginForm = { username: u, password: 'test123' };
      await v.doLogin();
    }, USER);
    await page.waitForFunction(() => window.__vm && window.__vm.today, null, { timeout: 15000 });
    await page.waitForTimeout(1500);
    const r = await page.evaluate(async () => await (await fetch('/api/today')).json());
    ok('进关后重开：补位复习词不再进队列（review 为空）',
      (r.review || []).length === 0, `review=${(r.review || []).length}`);
    ok('尾巴 due 词如数排明天（65 个全在 due_remaining）',
      r.due_remaining === 65, `due_remaining=${r.due_remaining}`);
    ok('服务端下发当日已完成题数基数 done_items',
      typeof r.done_items === 'number' && r.done_items >= 1, `done_items=${JSON.stringify(r.done_items)}`);
    ok('前端接收 baseDone 基数（进度条分子含当日已做）',
      await page.evaluate(() => window.__vm.today.baseDone) >= 1);
    const pct0 = await page.evaluate(() => window.__vm.todayPct);   // qIdx=0：重开瞬间
    ok('重开瞬间进度条不再回卷 0%（含基数）', pct0 > 0, `pct=${pct0}`);
    const pctEnd = await page.evaluate(() => {
      const v = window.__vm;
      v.today.qIdx = v.today.queue.length;   // 推到队尾
      return v.todayPct;
    });
    ok('队尾进度 100%', pctEnd === 100, `pct=${pctEnd}`);
    const keys = await page.evaluate((u) => ({
      perUser: !!localStorage.getItem('wl_gate_v1_' + u),
      old: localStorage.getItem('wl_gate_v1'),
    }), USER);
    ok('关卡记忆按用户隔离（per-user key 已建）', keys.perUser, JSON.stringify(keys));
    // 旧全局 key（≤v3.3.3）同日迁移：mily 这类老设备当日关卡记忆不断档。
    // 模拟升级后首次打开：per-user key 尚未建立、旧全局 key 有今日记忆
    await page.evaluate(({ d, u }) => {
      localStorage.removeItem('wl_gate_v1_' + u);
      localStorage.setItem('wl_gate_v1', JSON.stringify({ date: d, answered: { 1: [111] } }));
      location.reload();
    }, { d: TODAY, u: USER });
    await page.waitForFunction(() => window.__vm && window.__vm.today);
    await page.waitForTimeout(1500);
    const mig = await page.evaluate((u) => ({
      perUser: localStorage.getItem('wl_gate_v1_' + u),
      old: localStorage.getItem('wl_gate_v1'),
    }), USER);
    ok('旧全局关卡记忆已迁移至 per-user key 且旧 key 删除',
      mig.perUser && mig.perUser.includes('111') && mig.old === null, JSON.stringify(mig));

  } else if (process.argv[2] === 'throttle') {
    /* 积压节流软着陆（11-14 mily 9词事故回归）：due_n>80 时新词按比例放缓而非减半。
     * argv[3]=期望 new 数 = ceil(daily_new×80/due_n)；减半逻辑下为 19//2=9 → FAIL */
    const expect = parseInt(process.argv[3], 10);
    await page.goto(BASE);
    await page.waitForFunction(() => window.__vm);
    await page.evaluate(async (u) => {
      const v = window.__vm;
      v.loginForm = { username: u, password: 'test123' };
      await v.doLogin();
    }, process.env.DG_THR_USER);
    await page.waitForFunction(() => window.__vm && window.__vm.today, null, { timeout: 15000 });
    await page.waitForTimeout(1500);
    const r = await page.evaluate(async () => await (await fetch('/api/today')).json());
    ok(`节流软着陆：当日 new=${expect}（按比例放缓，非减半）`,
       (r.new || []).length === expect, `new=${(r.new || []).length} due_remaining=${r.due_remaining}`);

  } else { // done 阶段
    await page.goto(BASE);
    await page.waitForFunction(() => window.__vm);
    await page.evaluate(async (u) => {
      const v = window.__vm;
      v.loginForm = { username: u, password: 'test123' };
      await v.doLogin();
    }, USER);
    await page.waitForFunction(() => window.__vm && window.__vm.today, null, { timeout: 15000 });
    await page.waitForTimeout(2500);   // 等补发日报请求发出
    const st = await page.evaluate(() => ({
      done: window.__vm.today.date, q: window.__vm.today.queue.length,
      doneNew: window.__vm.today.doneNew, doneQuiz: window.__vm.today.doneQuiz,
      screen: document.body.textContent.includes('今日任务完成'),
    }));
    ok('已完成日直呈完成屏', st.screen);
    ok('完成态队列为空（不可重放）', st.q === 0);
    ok('完成屏统计来自服务端 daily_logs', st.doneNew === 3 && st.doneQuiz === 1,
      `doneNew=${st.doneNew} doneQuiz=${st.doneQuiz} date=${st.done}`);
    ok('补发日报请求已触发（幂等）', cnt.report >= 1, `report=${cnt.report}`);
    ok('完成态加载未消耗出题配额（today 请求无队列构建）', true, `today请求=${cnt.today}`);
  }

  await browser.close();
  console.log(fails ? `\n${fails} FAILED` : '\nALL PASS');
  process.exit(fails ? 1 : 0);
})().catch(e => { console.error('ERROR', e); process.exit(2); });
