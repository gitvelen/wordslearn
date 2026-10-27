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
