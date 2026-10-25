"""端到端 API 冒烟（v4.2）：临时测试用户 → confirm → 计划 → 今日队列（4题链+巩固题）→
状态机全走查（D+2→D+5→D+12→mastered，答错回炉）→ 清理。
容器内运行：
  docker cp tests/e2e.py wordslearn:/tmp/e2e.py && docker exec wordslearn python /tmp/e2e.py
本地运行（默认 http://localhost:8091，库 ../app/data/wordslearn.db）：
  python3 tests/e2e.py
"""
import os, json, sqlite3, urllib.request, urllib.error, http.cookiejar, sys, uuid, bcrypt

BASE = os.environ.get('E2E_BASE', 'http://localhost:8091')
DB_PATH = os.environ.get('E2E_DB', os.path.join(os.path.dirname(__file__), '..', 'app', 'data', 'wordslearn.db'))

db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
TODAY = sqlite3.Row  # placeholder for lint clarity
import datetime
TODAY_STR = datetime.date.today().isoformat()

def api(cj, path, method='GET', body=None):
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    req = urllib.request.Request(BASE + path, method=method)
    if body is not None:
        req.add_header('Content-Type', 'application/json')
        req.data = json.dumps(body).encode()
    try:
        r = opener.open(req, timeout=10)
        return r.status, json.loads(r.read() or b'{}')
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b'{}')

FAILS = []
def check(name, cond, extra=''):
    print(('✓' if cond else '✗ FAIL'), name, ('| ' + str(extra)) if extra else '')
    if not cond:
        FAILS.append(name)

def mk_user(tag, grade=None):
    uname = f'e2e_{tag}_' + uuid.uuid4().hex[:6]
    db.execute("INSERT INTO users(username, password_hash, display_name, mode) VALUES(?,?,?,'standard')",
               (uname, bcrypt.hashpw(b'test123', bcrypt.gensalt()).decode(), tag))
    if grade:
        db.execute("UPDATE users SET grade=?, mode=? WHERE username=?",
                   (grade, 'junior' if grade in ('1A','1B','2A','2B') else 'standard', uname))
    db.commit()
    uid = db.execute("SELECT id FROM users WHERE username=?", (uname,)).fetchone()['id']
    cj = http.cookiejar.CookieJar()
    s, _ = api(cj, '/api/login', 'POST', {'username': uname, 'password': 'test123'})
    assert s == 200, f'login fail {uname}'
    return uname, uid, cj

def st(uid, wid):
    return db.execute("SELECT * FROM user_word_state WHERE user_id=? AND word_id=?", (uid, wid)).fetchone()

def force_due(uid, wid, stage):
    """把复习日期改到今天，模拟间隔到达。"""
    db.execute("UPDATE user_word_state SET next_review_date=?, stage=? WHERE user_id=? AND word_id=?",
               (TODAY_STR, stage, uid, wid))
    db.commit()

# ================= 画像 A：standard 全流程 =================
uname, uid, cj = mk_user('std', '5A')

s, r = api(cj, '/api/me')
check('me: 5A standard 需确认', s == 200 and r['need_confirm'] and r['need_basic_quiz'])

# ---- confirm：快测 15 题 → 全对 → 基础段整批 skipped ----
s, r = api(cj, '/api/confirm/basic_quiz')
check('快测出 15 题', s == 200 and len(r['quiz']) == 15, len(r.get('quiz', [])))
answers = {str(q['word_id']): True for q in r['quiz']}
s, r = api(cj, '/api/confirm/basic_result', 'POST', {'answers': answers})
check('快测全对 → 通过并整段跳过', s == 200 and r['passed'] and r['skipped_total'] > 100,
      r.get('skipped_total'))
s, r = api(cj, '/api/confirm/basic_quiz')
check('快测重复拉取为空', s == 200 and r['quiz'] == [])

# ---- confirm：速选表格 → 提交 ----
s, r = api(cj, '/api/confirm/pool?size=200')
check('表格池出词（词频序）', s == 200 and 100 <= len(r['words']) <= 200, len(r.get('words', [])))
known = [w['id'] for w in r['words'][:5]]
s, _ = api(cj, '/api/confirm/submit', 'POST', {'known_ids': known})
s, r = api(cj, '/api/confirm/status')
check('速选提交生效', s == 200 and r['skipped'] > 100 and r['verify'] == 0, dict(r))
s, _ = api(cj, '/api/confirm/finish', 'POST')
s, r = api(cj, '/api/me')
check('confirm_done', not r['need_confirm'])

# ---- 计划 ----
s, r = api(cj, '/api/plan', 'POST', {'target_words': 40, 'days': 20})
check('建计划成功', s == 200 and r['daily_new'] >= 5, json.dumps(r, ensure_ascii=False))
plan_id = r['plan_id']
s, r = api(cj, '/api/plan', 'POST', {'target_words': 40, 'days': 20})
check('重复建计划被拒', s == 400)

# ---- today：4 题链 + 巩固题 ----
s, r = api(cj, '/api/today')
n0 = r['new'][0]
check('新词首学 4 题（word_zh/listen_zh/zh_word/spell）',
      [q['quiz_type'] for q in n0['quizzes']] == ['word_zh', 'listen_zh', 'zh_word', 'spell'],
      [q['quiz_type'] for q in n0['quizzes']])
check('巩固题已预取（d5/d2 借用）', bool(n0['reinforce']) and n0['reinforce']['quiz_type'] in ('cloze', 'zh_word', 'spell', 'word_zh'),
      n0['reinforce'] and n0['reinforce']['quiz_type'])
check('新词带义项（senses）', bool(n0['senses']))
wid = n0['id']

# ---- learn 提交 ----
s, _ = api(cj, '/api/answer', 'POST', {'word_id': wid, 'phase': 'learn', 'correct': True})
row = st(uid, wid)
check('learn → learning/stage1/next=D+2', row['state'] == 'learning' and row['stage'] == 1
      and row['next_review_date'] == (datetime.date.today() + datetime.timedelta(days=2)).isoformat())
log = db.execute("SELECT new_done FROM daily_logs WHERE user_id=? AND date=?", (uid, TODAY_STR)).fetchone()
check('daily_logs 计 1 新词', log and log['new_done'] == 1)

# ---- 复习状态机全走查：D+2→D+5→D+12→mastered ----
force_due(uid, wid, 1)
s, _ = api(cj, '/api/answer', 'POST', {'word_id': wid, 'phase': 'review', 'correct': True})
check('D+2 答对 → stage2', st(uid, wid)['stage'] == 2)
force_due(uid, wid, 2)
s, _ = api(cj, '/api/answer', 'POST', {'word_id': wid, 'phase': 'review', 'correct': True})
check('D+5 答对 → stage3', st(uid, wid)['stage'] == 3)
force_due(uid, wid, 3)
s, _ = api(cj, '/api/answer', 'POST', {'word_id': wid, 'phase': 'review', 'correct': True})
row = st(uid, wid)
check('D+12 答对 → mastered 且退池', row['state'] == 'mastered' and row['next_review_date'] is None)

# ---- 答错回炉 ----
wid2 = api(cj, '/api/today')[1]['new'][1]['id'] if len(api(cj, '/api/today')[1]['new']) > 1 else None
if wid2:
    api(cj, '/api/answer', 'POST', {'word_id': wid2, 'phase': 'learn', 'correct': True})
    force_due(uid, wid2, 2)
    api(cj, '/api/answer', 'POST', {'word_id': wid2, 'phase': 'review', 'correct': False})
    row = st(uid, wid2)
    check('复习答错 → 回炉 stage1/wrong+1', row['stage'] == 1 and row['wrong_count'] == 1)

# ---- word_detail ----
s, r = api(cj, '/api/word_detail?id=%d' % wid)
check('word_detail 含义项', s == 200 and r['word'] and isinstance(r['senses'], list) and 'forms' in r)

# ---- streak：纯复习日也续 ----
db.execute("UPDATE daily_logs SET new_done=0 WHERE user_id=? AND date=?", (uid, TODAY_STR))
db.commit()
s, r = api(cj, '/api/progress')
check('纯复习日 streak 续（review_done 计入）', r['streak'] >= 1, r.get('streak'))

# ---- calendar ----
s, r = api(cj, '/api/calendar?month=' + TODAY_STR[:7])
day = next((d for d in r['days'] if d['date'] == TODAY_STR), None)
check('calendar 今日条目', s == 200 and day is not None and day['in_plan'])

# ---- 学习日报：埋点 → 聚合 → 幂等发送 ----
# 热身 2 题 + 第4关听写 3 题（1 错）+ 补测 1 题（对）
wid0 = db.execute("SELECT word_id FROM user_word_state WHERE user_id=? LIMIT 1", (uid,)).fetchone()['word_id']
s, r = api(cj, '/api/quiz_log', 'POST', {'entries': [
    {'word_id': wid0, 'phase': 'warmup', 'quiz_type': 'word_zh', 'correct': True},
    {'word_id': wid0, 'phase': 'gate', 'level': 4, 'quiz_type': 'spell', 'correct': True},
    {'word_id': wid0, 'phase': 'gate', 'level': 4, 'quiz_type': 'spell', 'correct': False},
    {'word_id': wid0, 'phase': 'gate', 'level': 4, 'quiz_type': 'spell', 'correct': True, 'retry': True},
]})
check('quiz_log 埋点写入', s == 200)
n_log = db.execute("SELECT COUNT(*) c FROM quiz_log WHERE user_id=?", (uid,)).fetchone()['c']
check('learn/review 答案已入 quiz_log（/api/answer 埋点）', n_log >= 9, n_log)   # 5 状态机 + 4 手动
s, r = api(cj, '/api/report/preview')
rep = r['report']
rounds = {x['label']: x for x in rep['rounds']}
check('日报含热身/复习/关卡轮', all(k in rounds for k in ('晨间热身', '复习轮')) and any('听写' in k for k in rounds), list(rounds))
g4 = next((x for x in rep['rounds'] if '听写' in x['label']), None)
check('关卡首答正确率排除补测（2题1对=50%，补测1）',
      g4 and g4['n'] == 2 and g4['ok'] == 1 and g4['acc'] == 50 and g4['retries'] == 1, g4)
check('日报错词清单非空', len(rep['wrong_words']) >= 1, rep['wrong_words'])
check('日报给家长建议非空', len(rep['tips']) >= 1)
check('日报近7日趋势', len(rep['trend']) == 7 and rep['trend'][-1]['date'] == TODAY_STR)
s, r = api(cj, '/api/report/daily', 'POST')
check('日报发送（SMTP 未配置→不发送不置位）', s == 200 and r['sent'] is False and 'SMTP' in r['reason'], r)
flag = db.execute("SELECT report_sent FROM daily_logs WHERE user_id=? AND date=?", (uid, TODAY_STR)).fetchone()
check('未配置时 report_sent 不置位（可重试）', flag and flag['report_sent'] == 0)

# ================= 画像 B：junior（1A）=================
uname_j, uid_j, cj_j = mk_user('jr', '1A')
s, r = api(cj_j, '/api/me')
check('junior 无需确认', not r['need_confirm'])
s, r = api(cj_j, '/api/plan', 'POST', {'target_words': 30, 'days': 20})
check('junior 建计划', s == 200)
s, r = api(cj_j, '/api/today')
nj = r['new'][0]
jt = [q['quiz_type'] for q in nj['quizzes']]
check('junior 3 题链（emoji 或退化 zh_word）',
      jt == ['word_zh', 'listen_zh', 'emoji'] or jt == ['word_zh', 'listen_zh', 'zh_word'], jt)
check('junior 巩固题为 JUNIOR_TYPES 内', nj['reinforce']['quiz_type'] in ('word_zh', 'listen_zh', 'zh_word'),
      nj['reinforce']['quiz_type'])

# ---- 清理 ----
def _cleanup(uname):
    row = db.execute("SELECT id FROM users WHERE username=?", (uname,)).fetchone()
    if not row:
        return
    u = row['id']
    db.execute("DELETE FROM plan_words WHERE plan_id IN (SELECT id FROM study_plans WHERE user_id=?)", (u,))
    db.execute("DELETE FROM study_plans WHERE user_id=?", (u,))
    db.execute("DELETE FROM daily_logs WHERE user_id=?", (u,))
    db.execute("DELETE FROM user_word_state WHERE user_id=?", (u,))
    db.execute("DELETE FROM user_quiz_used WHERE user_id=?", (u,))
    db.execute("DELETE FROM quiz_log WHERE user_id=?", (u,))
    db.execute("DELETE FROM users WHERE id=?", (u,))

for _un in (uname, uname_j):
    _cleanup(_un)
db.commit(); db.close()

print()
print('❌ FAILED: ' + ', '.join(FAILS) if FAILS else '✅ ALL PASS (v4.2 API 冒烟通过)')
sys.exit(1 if FAILS else 0)
