/* v4.3 锚点展示测试：首学卡切分行/搭配行 + wordDetail 弹层切分/搭配 + syllAnswer 回退。
   数据不依赖全量 seed——dockerPy 直接给计划 seq=1 词注入 syllables/collocations。
   （spell 补测揭示态的完整误答链留待 seed 后真实数据人工验收。） */
const { chromium } = require('playwright-core');
const { execFileSync } = require('child_process');

const BASE = process.env.WL_BASE || 'https://words.velen.ltd';
const PW = 'Test123456';
const U = 'brtest_anchors';

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
for t in ('plan_words','study_plans','daily_logs','quiz_log','user_word_state'):
    db.execute("DELETE FROM %s WHERE user_id IN (SELECT id FROM users WHERE username='${U}')" % t
               if t != 'plan_words' else
               "DELETE FROM plan_words WHERE plan_id IN (SELECT id FROM study_plans WHERE user_id IN (SELECT id FROM users WHERE username='${U}'))")
db.execute("DELETE FROM users WHERE username='${U}'")
db.execute("INSERT INTO users(username,password_hash,display_name,mode,grade,confirm_done,basic_quiz_done,screening_skipped) VALUES(?,?,'Anch','standard','5A',1,1,1)", ('${U}', h))
db.commit()`);

  const browser = await chromium.launch({
    executablePath: '/opt/google-chrome/chrome',
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--autoplay-policy=no-user-gesture-required'],
  });
  const page = await (await browser.newContext({ viewport: { width: 820, height: 1200 } })).newPage();

  console.log('— 1. 登录建计划 —');
  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  await page.fill('input[placeholder="用户名"]', U);
  await page.fill('input[placeholder="密码"]', PW);
  await page.click('button:has-text("进入")');
  await page.waitForSelector('h2:has-text("制定你的计划")', { timeout: 8000 });
  await page.locator('input[type=number]').nth(0).fill('10');
  await page.locator('input[type=number]').nth(1).fill('17');
  await page.click('button:has-text("开始！")');
  await page.waitForSelector('.phase-tag, button:has-text("开始学习")', { timeout: 8000 });
  await page.waitForTimeout(1500);

  console.log('— 2. 注入锚点数据到 seq=1 词 —');
  // 切分块只为驱动前端渲染（拼接必须还原原词）；展示内容正确性由 gen_anchors 管线保证
  const inj = dockerPy(`import sqlite3, json
db = sqlite3.connect('/app/data/wordslearn.db')
r = db.execute("""SELECT w.id, w.word FROM plan_words p JOIN words w ON w.id=p.word_id
                  JOIN study_plans sp ON sp.id=p.plan_id
                  JOIN users u ON u.id=sp.user_id
                  WHERE u.username='${U}' ORDER BY p.seq LIMIT 1""").fetchone()
word = r[1]
syl = [word[:2]] if len(word) > 2 else None
if syl:
    rest = word[2:]
    while len(rest) > 2:
        syl.append(rest[:2]); rest = rest[2:]
    if rest: syl.append(rest)
    assert ''.join(syl) == word and len(syl) >= 2
db.execute("UPDATE words SET syllables=?, collocations=? WHERE id=?",
           (None if syl is None else json.dumps(syl, ensure_ascii=False),
            json.dumps([{"en":"a " + word + " test","zh":"一个测试"}], ensure_ascii=False), r[0]))
db.commit()
print(r[0], word)`).trim();
  const [wid, word] = inj.split(' ');
  console.log('  注入目标:', inj);

  console.log('— 3. 首学卡展示断言 —');
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.learn-card', { timeout: 8000 });
  // seq=1 词不一定是队首（新卡乱序 v3.2）——连播翻卡直到找到注入词或播完
  let found = false;
  for (let i = 0; i < 4; i++) {
    const cardWord = await page.locator('.learn-card .word-xl').first().textContent();
    if (cardWord.trim() === word) { found = true; break; }
    await page.click('button:has-text("记住了，下一个")').catch(() => {});
    await page.waitForTimeout(600);
  }
  check('注入词出现在首学卡', found, word);
  if (found) {
    const syll = await page.locator('.learn-card .syll-row').first().textContent().catch(() => '');
    check('首学卡切分行含 ·', syll.includes('·'), syll);
    const col = await page.locator('.learn-card .colloc-row').first().textContent().catch(() => '');
    check('首学卡搭配行展示', col.includes('💡'), col);
  }

  console.log('— 4. wordDetail 弹层断言 —');
  await page.evaluate((id) => app.openWordDetail(Number(id), 'wrong'), wid);
  await page.waitForSelector('.modal-card .syll-row', { timeout: 5000 });
  const msyll = await page.locator('.modal-card .syll-row').first().textContent();
  const mcol = await page.locator('.modal-card .colloc-row').first().textContent().catch(() => '');
  check('弹层切分行', msyll.includes('·'), msyll);
  check('弹层搭配行', mcol.includes('💡'), mcol);

  console.log('— 5. syllText/syllAnswer 纯函数 —');
  const fn = await page.evaluate(`JSON.stringify({
    s1: app.syllText({syllables: JSON.stringify(['op','por'])}),
    s2: app.syllText({syllables: JSON.stringify(['cat'])}),
    s3: app.syllText({syllables: null}),
    s4: app.syllText({syllables: 'bad json'}),
    a1: app.syllAnswer({syllables: JSON.stringify(['let','ter']), quiz:{answer:'letter'}}),
    a2: app.syllAnswer({syllables: null, quiz:{answer:'cat'}}) })`);
  const f = JSON.parse(fn);
  check('syllText 正常', f.s1 === 'op·por', f.s1);
  check('syllText 单块不展示', f.s2 === '', f.s2);
  check('syllText null 安全', f.s3 === '');
  check('syllText 坏 JSON 安全', f.s4 === '');
  check('syllAnswer 切分优先', f.a1 === 'let·ter', f.a1);
  check('syllAnswer 回退整词', f.a2 === 'cat', f.a2);

  await browser.close();
  console.log(FAILS.length ? `✗ ${FAILS.length} FAIL` : '✅ ALL PASS (anchors)');
  process.exit(FAILS.length ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });
