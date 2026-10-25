"""日报发送路径单测（mock SMTP，不真实外发）：
生成→发送→置位→幂等跳过。在仓库根目录跑：python3 tests/test-report.py
"""
import os
import sys
import sqlite3
import datetime
import unittest.mock as mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
import report  # noqa: E402

DB = os.path.join(os.path.dirname(__file__), '..', 'app', 'data', 'wordslearn.db')
SMTP_CONF = os.path.join(os.path.dirname(__file__), '..', 'app', 'data', 'smtp.json')
TODAY = datetime.date.today().isoformat()


def main():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    # 临时用户（绕开发信护栏），测完清理
    import bcrypt as _bcrypt
    old = db.execute("SELECT id FROM users WHERE username='wl_rt'").fetchone()  # wl_rt 不在 e2e_ 前缀内
    if old:
        db.execute("DELETE FROM quiz_log WHERE user_id=?", (old['id'],))
        db.execute("DELETE FROM daily_logs WHERE user_id=?", (old['id'],))
        db.execute("DELETE FROM users WHERE id=?", (old['id'],))
    uid = db.execute("INSERT INTO users(username,password_hash) VALUES('wl_rt', ?)",
                     (_bcrypt.hashpw(b'x', _bcrypt.gensalt()).decode(),)).lastrowid
    wid = db.execute("SELECT id FROM words LIMIT 1").fetchone()['id']
    # 造当日数据：2 复习（1 错）+ 关卡 2 首答（1 错）+ 1 补测（对）
    # → 积分 = 100 - 1×1 = 99（补测不返分）
    db.execute("INSERT INTO daily_logs(user_id, date, new_done, review_done) VALUES(?,?,1,2) "
               "ON CONFLICT(user_id, date) DO UPDATE SET new_done=1, review_done=2, report_sent=0, score=NULL", (uid, TODAY))
    db.execute("DELETE FROM quiz_log WHERE user_id=? AND date=?", (uid, TODAY))
    for phase, level, correct, retry in (('learn', None, 1, 0), ('review', None, 0, 0),
                                         ('review', None, 1, 0),
                                         ('gate', 4, 1, 0), ('gate', 4, 0, 0), ('gate', 4, 1, 1)):
        db.execute("INSERT INTO quiz_log(user_id, date, phase, level, word_id, quiz_type, correct, retry) "
                   "VALUES(?,?,?,?,?,?,?,?)", (uid, TODAY, phase, level, wid, 'spell', correct, retry))
    db.commit()
    db.close()

    # 临时 SMTP 配置（假主机；SMTP_SSL mock 掉不真实连接）
    open(SMTP_CONF, 'w').write('{"host":"smtp.126.com","port":465,"user":"weilingme@126.com","pass":"x","to":"velenh@qq.com"}')
    sent_mails = []
    try:
        with mock.patch.object(report.smtplib, 'SMTP_SSL') as MS:
            inst = MS.return_value.__enter__.return_value
            inst.sendmail = lambda f, t, m: sent_mails.append((f, t, m))

            out = report.send_daily_report(uid)
            assert out['sent'] is True, out
            assert len(sent_mails) == 1, '应发送一封'
            f, t, raw = sent_mails[0]
            assert t == ['velenh@qq.com']
            # MIME 编码（主题 RFC2047、正文 base64）→ 解码后断言
            import email
            from email.header import decode_header
            m = email.message_from_string(raw)
            subj = ''.join(p.decode(c or 'utf-8') if isinstance(p, bytes) else p
                           for p, c in decode_header(m['Subject']))
            body = m.get_payload(decode=True).decode('utf-8')
            subj_ok = '学习日报' in subj and '99分' in subj and TODAY[5:] in subj
            html_ok = ('每日学习报告' in body and '听写' in body and '给家长的建议' in body
                       and '今日复习' in body and '今日得分' in body and '99' in body)
            print('✓ 发送成功：126发件→QQ收件，主题含得分，HTML 含复习板块', '(subj:', subj_ok, 'html:', html_ok, ')')
            assert subj_ok and html_ok

            db = sqlite3.connect(DB)
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT report_sent, score FROM daily_logs WHERE user_id=? AND date=?",
                             (uid, TODAY)).fetchone()
            db.close()
            assert row['report_sent'] == 1 and row['score'] == 99
            print('✓ report_sent 置位 + 积分结算 99（100-1）')

            out2 = report.send_daily_report(uid)
            assert out2['sent'] is False and '已发送' in out2['reason'], out2
            assert len(sent_mails) == 1, '幂等：不重发'
            print('✓ 二次调用幂等跳过（积分与邮件均不变）')
    finally:
        os.remove(SMTP_CONF)
        # 恢复 126 占位配置（pass 空=未配置态）
        open(SMTP_CONF, 'w').write('{"host": "smtp.126.com", "port": 465, "user": "weilingme@126.com", "pass": "", "to": "velenh@qq.com"}')
        db = sqlite3.connect(DB)
        db.execute("DELETE FROM quiz_log WHERE user_id=?", (uid,))
        db.execute("DELETE FROM daily_logs WHERE user_id=?", (uid,))
        db.execute("DELETE FROM users WHERE id=?", (uid,))
        db.commit()
        db.close()
    print('✅ ALL PASS (report 发送路径单测)')


if __name__ == '__main__':
    main()
