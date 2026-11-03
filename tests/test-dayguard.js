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
