#!/bin/bash
# listen_def（听英文释义选单词）e2e 编排（v3.4）
# 前置：8091 测试服务已跑（WL_DB=/tmp/wl-dayguard/data/wordslearn.db，新代码，listen_def 题已导入）
# 用法：bash tests/test-listen-def.sh
set -e
cd "$(dirname "$0")/.."
DB=${LD_DB:-/tmp/wl-dayguard/data/wordslearn.db}

# 预设：standard 用户（今日 3 新词：多义/单义/无def词 + 昨日 2 词：多义/单义热身）
#        junior 用户（1A，今日同 3 词 + 昨日 1 多义词）
OUT=$(python3 - "$DB" <<'EOF'
import sqlite3, uuid, bcrypt, datetime, sys, json
db = sqlite3.connect(sys.argv[1]); db.row_factory = sqlite3.Row

def pick(excl):
    multi = db.execute("""SELECT wd.id, wd.word FROM words wd
        WHERE EXISTS (SELECT 1 FROM word_quiz q WHERE q.word_id=wd.id AND q.quiz_type='listen_def' AND q.idx=1)
          AND EXISTS (SELECT 1 FROM word_quiz q WHERE q.word_id=wd.id AND q.quiz_type='listen_def' AND q.idx=2)
          AND NOT EXISTS (SELECT 1 FROM word_quiz q WHERE q.word_id=wd.id AND q.quiz_type='listen_def' AND idx>=3)
          AND wd.id NOT IN (%s) LIMIT 1""" % (excl or '0'), ).fetchone()
    single = db.execute("""SELECT wd.id, wd.word FROM words wd
        WHERE EXISTS (SELECT 1 FROM word_quiz q WHERE q.word_id=wd.id AND q.quiz_type='listen_def' AND q.idx=1)
          AND NOT EXISTS (SELECT 1 FROM word_quiz q WHERE q.word_id=wd.id AND q.quiz_type='listen_def' AND idx>=2)
          AND wd.id NOT IN (%s) LIMIT 1""" % (excl or '0'), ).fetchone()
    nodef = db.execute("""SELECT wd.id, wd.word FROM words wd
        WHERE EXISTS (SELECT 1 FROM word_quiz q WHERE q.word_id=wd.id)
          AND NOT EXISTS (SELECT 1 FROM word_quiz q WHERE q.word_id=wd.id AND q.quiz_type='listen_def')
          AND wd.id NOT IN (%s) LIMIT 1""" % (excl or '0'), ).fetchone()
    return multi, single, nodef

t = datetime.date.today()
yst = (t - datetime.timedelta(days=1)).isoformat()
out = {}
for mode, grade, tag in (('standard', '5A', 'std'), ('junior', '1A', 'jr')):
    m1, s1, n1 = pick('0')
    m2, s2, _ = pick(','.join(str(x) for x in (m1['id'], s1['id'], n1['id'])))
    uname = f'e2e_ld_{tag}_' + uuid.uuid4().hex[:6]
    db.execute("INSERT INTO users(username, password_hash, display_name, mode, grade, confirm_done, basic_quiz_done)"
               " VALUES(?,?,?,?,?,1,1)",
               (uname, bcrypt.hashpw(b'test123', bcrypt.gensalt()).decode(), 'ld', mode, grade))
    uid = db.execute("SELECT id FROM users WHERE username=?", (uname,)).fetchone()['id']
    db.execute("INSERT INTO study_plans(user_id, status, target_words, start_date, end_date, daily_new, created_at)"
               " VALUES(?, 'active', 3, ?, ?, 3, datetime('now'))",
               (uid, t.isoformat(), (t + datetime.timedelta(days=30)).isoformat()))
    pid = db.execute("SELECT id FROM study_plans WHERE user_id=? AND status='active'", (uid,)).fetchone()['id']
    for w, i in ((m1, 1), (s1, 2), (n1, 3)):
        db.execute("INSERT INTO plan_words(plan_id, word_id, seq) VALUES(?,?,?)", (pid, w['id'], i))
    # 昨日词（今日热身）：多义 m2 + 单义 s2（junior 只放 1 个也够断言）
    for w in ((m2, s2) if mode == 'standard' else (m2,)):
        db.execute("""INSERT INTO user_word_state(user_id,word_id,state,stage,first_learned_date,next_review_date,wrong_count,verify_count)
                      VALUES(?,?,'learning',1,?,?,0,0)""", (uid, w['id'], yst, t.isoformat()))
    out[tag] = uname
db.commit(); db.close()
print(out['std'], out['jr'])
EOF
)
LD_USER=$(echo $OUT | cut -d' ' -f1)
LD_JR_USER=$(echo $OUT | cut -d' ' -f2)
export LD_USER LD_JR_USER
export DG_BASE=${DG_BASE:-http://localhost:8091}
echo "测试用户: std=$LD_USER jr=$LD_JR_USER"

echo "===== gate 阶段（第 6 关下发/跳关/音频抓包/会话记忆/兜底）====="
node tests/test-listen-def.js gate

echo "===== warmup 阶段（多义展开/单义回落/哨兵防重放）====="
node tests/test-listen-def.js warmup

echo "===== junior 阶段（无第 6 关/热身无 def）====="
node tests/test-listen-def.js junior

echo "===== 清理测试用户 ====="
python3 - "$DB" "$LD_USER" "$LD_JR_USER" <<'EOF'
import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
for uname in sys.argv[2:]:
    u = db.execute("SELECT id FROM users WHERE username=?", (uname,)).fetchone()
    if not u: continue
    u = u[0]
    db.execute("DELETE FROM quiz_log WHERE user_id=?", (u,))
    db.execute("DELETE FROM user_quiz_used WHERE user_id=?", (u,))
    db.execute("DELETE FROM user_word_state WHERE user_id=?", (u,))
    db.execute("DELETE FROM plan_words WHERE plan_id IN (SELECT id FROM study_plans WHERE user_id=?)", (u,))
    db.execute("DELETE FROM study_plans WHERE user_id=?", (u,))
    db.execute("DELETE FROM daily_logs WHERE user_id=?", (u,))
    db.execute("DELETE FROM audit_schedule WHERE user_id=?", (u,))
    db.execute("DELETE FROM users WHERE id=?", (u,))
db.commit(); db.close()
print("✓ 测试用户已清理")
EOF
echo "===== 全部通过 ====="
