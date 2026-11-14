#!/bin/bash
# 隔夜复活/同日重开守卫 e2e 编排（v2.7.5）
# 前置：/tmp/wl-dayguard 测试服务已跑在 8091（库为生产副本、无 smtp.json）
# 用法：bash tests/test-dayguard.sh
set -e
cd "$(dirname "$0")/.."
DB=/tmp/wl-dayguard/data/wordslearn.db
DG_USER=$(python3 - "$DB" <<'EOF'
import sqlite3, uuid, bcrypt, datetime, sys
db = sqlite3.connect(sys.argv[1]); db.row_factory = sqlite3.Row
uname = 'e2e_dg_' + uuid.uuid4().hex[:6]
db.execute("INSERT INTO users(username, password_hash, display_name, mode, grade, confirm_done, basic_quiz_done)"
           " VALUES(?,?,?,'standard','5A',1,1)",
           (uname, bcrypt.hashpw(b'test123', bcrypt.gensalt()).decode(), 'dg'))
uid = db.execute("SELECT id FROM users WHERE username=?", (uname,)).fetchone()['id']
t = datetime.date.today()
db.execute("INSERT INTO study_plans(user_id, status, target_words, start_date, end_date, daily_new, created_at)"
           " VALUES(?, 'active', 8, ?, ?, 4, datetime('now'))", (uid, t.isoformat(), (t+datetime.timedelta(days=30)).isoformat()))
pid = db.execute("SELECT id FROM study_plans WHERE user_id=? AND status='active'", (uid,)).fetchone()['id']
words = [r['word_id'] for r in db.execute(
    "SELECT DISTINCT word_id FROM word_quiz JOIN words ON words.id=word_quiz.word_id"
    " WHERE words.emoji IS NOT NULL ORDER BY word_id LIMIT 8")]
db.executemany("INSERT INTO plan_words(plan_id, word_id, seq) VALUES(?,?,?)",
               [(pid, w, i+1) for i, w in enumerate(words)])
db.commit()
print(uname)
EOF
)
echo "测试用户: $DG_USER"
export DG_USER DG_TODAY=$(date +%F) DG_BASE=http://localhost:8091

echo "===== main 阶段（复活守卫）====="
node tests/test-dayguard.js main

echo "===== midway 阶段（中途退出重开——11-03 事故回归）====="
python3 -c "
import sqlite3,datetime
db=sqlite3.connect('$DB')
u=db.execute(\"SELECT id FROM users WHERE username='$DG_USER'\").fetchone()[0]
db.execute('INSERT INTO daily_logs(user_id,date,new_done,review_done,minutes,report_sent,score) VALUES(?,?,3,0,0,0,NULL)',
           (u, datetime.date.today().isoformat()))
db.commit()"
node tests/test-dayguard.js midway
python3 -c "
import sqlite3,datetime
db=sqlite3.connect('$DB')
u=db.execute(\"SELECT id FROM users WHERE username='$DG_USER'\").fetchone()[0]
db.execute('DELETE FROM daily_logs WHERE user_id=? AND date=?',(u,datetime.date.today().isoformat()))
db.commit()"

echo "===== gate 阶段（进关后重开不补位+进度条基数——11-12 mily 事故回归）====="
python3 -c "
import sqlite3, datetime
db = sqlite3.connect('$DB')
u = db.execute(\"SELECT id FROM users WHERE username='$DG_USER'\").fetchone()[0]
t = datetime.date.today().isoformat()
# 造 65 个 due 词（> REVIEW_CAP=60）：plan 外选词避免覆盖 main 阶段学的词
words = [r[0] for r in db.execute(
    'SELECT id FROM words WHERE id NOT IN (SELECT word_id FROM plan_words'
    ' WHERE plan_id=(SELECT id FROM study_plans WHERE user_id=? AND status=\\'active\\'))'
    ' ORDER BY id LIMIT 65', (u,)).fetchall()]
assert len(words) == 65, f'词不够: {len(words)}'
db.executemany(\"INSERT OR REPLACE INTO user_word_state(user_id,word_id,state,stage,first_learned_date,next_review_date,wrong_count,verify_count) VALUES(?,?,'learning',2,?,?,0,0)\",
               [(u, w, (datetime.date.today()-datetime.timedelta(days=7)).isoformat(), t) for w in words])
# 当日已有 gate 埋点 = 已进入关卡（11-12 mily 退出点在 gate4）
pw = db.execute('SELECT word_id FROM plan_words WHERE plan_id=(SELECT id FROM study_plans WHERE user_id=? AND status=\\'active\\') LIMIT 1', (u,)).fetchone()[0]
db.execute(\"INSERT INTO quiz_log(user_id,date,phase,word_id,quiz_type,correct) VALUES(?,?,'gate',?,'word_zh',1)\", (u, t, pw))
db.commit()"
node tests/test-dayguard.js gate
python3 -c "
import sqlite3, datetime
db = sqlite3.connect('$DB')
u = db.execute(\"SELECT id FROM users WHERE username='$DG_USER'\").fetchone()[0]
db.execute('DELETE FROM quiz_log WHERE user_id=? AND date=?', (u, datetime.date.today().isoformat()))
db.execute(\"DELETE FROM user_word_state WHERE user_id=? AND first_learned_date=?\",
           (u, (datetime.date.today()-datetime.timedelta(days=7)).isoformat()))
db.commit()"

echo "===== throttle 阶段（积压节流软着陆——11-14 mily 9词事故回归）====="
THR_USER=$(python3 - "$DB" <<'EOF'
import sqlite3, uuid, bcrypt, datetime, sys
db = sqlite3.connect(sys.argv[1]); db.row_factory = sqlite3.Row
uname = 'e2e_thr_' + uuid.uuid4().hex[:6]
db.execute("INSERT INTO users(username, password_hash, display_name, mode, grade, confirm_done, basic_quiz_done)"
           " VALUES(?,?,?,'standard','5A',1,1)",
           (uname, bcrypt.hashpw(b'test123', bcrypt.gensalt()).decode(), 'thr'))
uid = db.execute("SELECT id FROM users WHERE username=?", (uname,)).fetchone()['id']
t = datetime.date.today()
db.execute("INSERT INTO study_plans(user_id, status, target_words, start_date, end_date, daily_new, created_at)"
           " VALUES(?, 'active', 30, ?, ?, 19, datetime('now'))", (uid, t.isoformat(), (t+datetime.timedelta(days=30)).isoformat()))
pid = db.execute("SELECT id FROM study_plans WHERE user_id=? AND status='active'", (uid,)).fetchone()['id']
words = [r['word_id'] for r in db.execute(
    "SELECT DISTINCT word_id FROM word_quiz JOIN words ON words.id=word_quiz.word_id"
    " WHERE words.emoji IS NOT NULL ORDER BY word_id LIMIT 30")]
assert len(words) == 30, f'词不够: {len(words)}'
db.executemany("INSERT INTO plan_words(plan_id, word_id, seq) VALUES(?,?,?)",
               [(pid, w, i+1) for i, w in enumerate(words)])
# 104 个 plan 外 due 词（复刻 11-14 mily 现场：实发 60 + 尾巴 44 = 104 > 80 节流线）
due = [r[0] for r in db.execute(
    'SELECT id FROM words WHERE id NOT IN (SELECT word_id FROM plan_words WHERE plan_id=?)'
    ' ORDER BY id LIMIT 104', (pid,)).fetchall()]
assert len(due) == 104, f'词不够: {len(due)}'
db.executemany("INSERT OR REPLACE INTO user_word_state(user_id,word_id,state,stage,first_learned_date,next_review_date,wrong_count,verify_count) VALUES(?,?,'learning',2,?,?,0,0)",
               [(uid, w, (t-datetime.timedelta(days=7)).isoformat(), t.isoformat()) for w in due])
db.commit()
print(uname)
EOF
)
export DG_THR_USER=$THR_USER
node tests/test-dayguard.js throttle 19    # 104 < 110(NEW_THROTTLE_AT) 不触发，满额19（80线时代 15 → FAIL）
python3 -c "
import sqlite3, datetime
db = sqlite3.connect('$DB')
u = db.execute(\"SELECT id FROM users WHERE username='$THR_USER'\").fetchone()[0]
pid = db.execute(\"SELECT id FROM study_plans WHERE user_id=? AND status='active'\", (u,)).fetchone()[0]
t = datetime.date.today().isoformat()
# 场景2：再插 26 个 due（104→130，模拟缺勤2天积压）→ ceil(19×110/130)=17 缓降
more = [r[0] for r in db.execute(
    'SELECT id FROM words WHERE id NOT IN (SELECT word_id FROM plan_words WHERE plan_id=?)'
    ' ORDER BY id LIMIT 26 OFFSET 104', (pid,)).fetchall()]
assert len(more) == 26, f'词不够: {len(more)}'
db.executemany(\"INSERT OR REPLACE INTO user_word_state(user_id,word_id,state,stage,first_learned_date,next_review_date,wrong_count,verify_count) VALUES(?,?,'learning',2,?,?,0,0)\",
               [(u, w, (datetime.date.today()-datetime.timedelta(days=7)).isoformat(), t) for w in more])
db.commit()"
node tests/test-dayguard.js throttle 17    # ceil(19×110/130)=17
python3 -c "
import sqlite3
db = sqlite3.connect('$DB')
u = db.execute(\"SELECT id FROM users WHERE username='$THR_USER'\").fetchone()[0]
db.execute('DELETE FROM user_word_state WHERE user_id=?', (u,))
db.execute('DELETE FROM plan_words WHERE plan_id IN (SELECT id FROM study_plans WHERE user_id=?)', (u,))
db.execute('DELETE FROM study_plans WHERE user_id=?', (u,))
db.execute('DELETE FROM audit_schedule WHERE user_id=?', (u,))
db.execute('DELETE FROM user_quiz_used WHERE user_id=?', (u,))
db.execute('DELETE FROM users WHERE id=?', (u,))
db.commit()"

echo "===== 造完成态（daily_logs score=100 report_sent=1）====="
USED_BEFORE=$(python3 -c "
import sqlite3,sys
db=sqlite3.connect('$DB')
u=db.execute(\"SELECT id FROM users WHERE username='$DG_USER'\").fetchone()[0]
print(db.execute('SELECT COUNT(*) FROM user_quiz_used WHERE user_id=?',(u,)).fetchone()[0])")
python3 -c "
import sqlite3,datetime
db=sqlite3.connect('$DB')
u=db.execute(\"SELECT id FROM users WHERE username='$DG_USER'\").fetchone()[0]
db.execute('INSERT INTO daily_logs(user_id,date,new_done,review_done,minutes,report_sent,score) VALUES(?,?,3,0,0,1,100)',
           (u, datetime.date.today().isoformat()))
db.commit()"

echo "===== done 阶段（完成日重开）====="
node tests/test-dayguard.js done

echo "===== 服务端侧验证 ====="
python3 -c "
import sqlite3,sys
db=sqlite3.connect('$DB'); db.row_factory=sqlite3.Row
u=db.execute(\"SELECT id FROM users WHERE username='$DG_USER'\").fetchone()['id']
used=db.execute('SELECT COUNT(*) c FROM user_quiz_used WHERE user_id=?',(u,)).fetchone()['c']
dl=db.execute('SELECT * FROM daily_logs WHERE user_id=?',(u,)).fetchone()
assert used == $USED_BEFORE, f'完成态加载消耗了出题配额: {used} > $USED_BEFORE'
print('✓ 完成态加载零出题消耗 (user_quiz_used 不变: %d)' % used)
assert dl['report_sent']==1 and dl['score']==100, '补发不该改写已完成结算'
print('✓ 补发幂等：report_sent=1 score=100 未被改写')
n=db.execute(\"SELECT COUNT(*) c FROM quiz_log WHERE user_id=? AND date=date('now')\",(u,)).fetchone()['c']
assert n==0, f'完成日重开产生了作答记录 {n} 条'
print('✓ 完成日重开零作答记录（不可重放）')
# v3.3.5 时区统一 Asia/Shanghai：schema 默认值 localtime + 新写入为 CST
schema = db.execute(\"SELECT sql FROM sqlite_master WHERE name='quiz_log'\").fetchone()[0]
assert 'localtime' in schema, 'quiz_log 默认值未迁移到 localtime'
print('✓ quiz_log 时间默认值已统一 localtime(Asia/Shanghai)')
db.execute(\"INSERT INTO quiz_log(user_id,date,phase,word_id,quiz_type,correct) VALUES(?,date('now','localtime'),'warmup',556,'word_zh',1)\", (u,))
ts = db.execute('SELECT ts FROM quiz_log WHERE rowid=last_insert_rowid()').fetchone()[0]
local = db.execute(\"SELECT datetime('now','localtime')\").fetchone()[0]
db.execute('DELETE FROM quiz_log WHERE rowid=last_insert_rowid()'); db.commit()
diff = abs(db.execute(\"SELECT (julianday(?) - julianday(?)) * 86400\", (ts, local)).fetchone()[0])
assert diff < 120, f'新写入 ts={ts} 偏离本地时钟 {diff}s（仍在写 UTC）'
print(f'✓ 新写入时间戳为本地时区（ts={ts}）')
db.execute('DELETE FROM quiz_log WHERE user_id=?',(u,))
db.execute('DELETE FROM user_quiz_used WHERE user_id=?',(u,))
db.execute('DELETE FROM plan_words WHERE plan_id IN (SELECT id FROM study_plans WHERE user_id=?)',(u,))
db.execute('DELETE FROM study_plans WHERE user_id=?',(u,))
db.execute('DELETE FROM daily_logs WHERE user_id=?',(u,))
db.execute('DELETE FROM user_word_state WHERE user_id=?',(u,))
db.execute('DELETE FROM users WHERE id=?',(u,))
db.commit()
print('✓ 测试用户已清理')
"
echo "===== 全部通过 ====="
