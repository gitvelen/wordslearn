"""重置用户学习状态（方案 B，v4.1）——用法: python3 reset_user.py mily

语义（与用户确认过的范围）：
- learning/mastered 的词 → 删除状态（回候选池，会出现在表格确认里重新挑——
  例如 mily 第一天学过的 19 个词，她现在认识，可在表格里点掉）
- state='new' 的词 → 保留（旧摸底确认过的生词，无需再确认，直接进计划池）
- state='skipped' → 保留（摸底产物，用户明示不清）
- daily_logs 全删、active 计划废弃、confirm_done 归零（重走表格确认）
"""
import sys
from database import get_db, init_db


def reset_user(username):
    init_db()
    db = get_db()
    try:
        user = db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if not user:
            print(f"用户不存在: {username}")
            return
        uid = user["id"]
        stats = {}
        for st in ("learning", "mastered", "verify"):
            cur = db.execute("DELETE FROM user_word_state WHERE user_id=? AND state=?", (uid, st))
            stats[st] = cur.rowcount
        kept_new = db.execute("SELECT COUNT(*) c FROM user_word_state WHERE user_id=? AND state='new'",
                              (uid,)).fetchone()["c"]
        kept_skipped = db.execute("SELECT COUNT(*) c FROM user_word_state WHERE user_id=? AND state='skipped'",
                                  (uid,)).fetchone()["c"]
        logs = db.execute("DELETE FROM daily_logs WHERE user_id=?", (uid,)).rowcount
        plans = db.execute("UPDATE study_plans SET status='abandoned' WHERE user_id=? AND status='active'",
                           (uid,)).rowcount
        db.execute("UPDATE users SET confirm_done=0 WHERE id=?", (uid,))
        db.execute("DELETE FROM user_quiz_used WHERE user_id=?", (uid,))
        quiz_logs = db.execute("DELETE FROM quiz_log WHERE user_id=?", (uid,)).rowcount
        db.commit()
        print(f"已重置 {username}: 删除 learning={stats['learning']} mastered={stats['mastered']} "
              f"verify={stats['verify']}，保留 new={kept_new} skipped={kept_skipped}，"
              f"删除日志 {logs} 天，废弃计划 {plans} 个，confirm_done=0，"
              f"删除作答记录 quiz_log={quiz_logs}")
    finally:
        db.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    reset_user(sys.argv[1])
