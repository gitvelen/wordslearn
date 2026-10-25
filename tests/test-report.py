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
    uid = db.execute("SELECT id FROM users WHERE username='test'").fetchone()['id']
    wid = db.execute("SELECT id FROM words LIMIT 1").fetchone()['id']
    # 造当日数据：1 新词 + 2 复习（1 错）+ 1 关卡补测
    db.execute("INSERT INTO daily_logs(user_id, date, new_done, review_done) VALUES(?,?,1,2) "
               "ON CONFLICT(user_id, date) DO UPDATE SET new_done=1, review_done=2, report_sent=0", (uid, TODAY))
    db.execute("DELETE FROM quiz_log WHERE user_id=? AND date=?", (uid, TODAY))
    for phase, level, correct, retry in (('learn', None, 1, 0), ('review', None, 0, 0),
                                         ('review', None, 1, 0), ('gate', 4, 1, 1)):
        db.execute("INSERT INTO quiz_log(user_id, date, phase, level, word_id, quiz_type, correct, retry) "
                   "VALUES(?,?,?,?,?,?,?,?)", (uid, TODAY, phase, level, wid, 'spell', correct, retry))
    db.commit()
    db.close()

    # 临时 SMTP 配置（假主机；SMTP_SSL mock 掉不真实连接）
    open(SMTP_CONF, 'w').write('{"host":"smtp.test","port":465,"user":"a@test.com","pass":"x","to":"p@test.com"}')
    sent_mails = []
    try:
        with mock.patch.object(report.smtplib, 'SMTP_SSL') as MS:
            inst = MS.return_value.__enter__.return_value
            inst.sendmail = lambda f, t, m: sent_mails.append((f, t, m))

            out = report.send_daily_report(uid)
            assert out['sent'] is True, out
            assert len(sent_mails) == 1, '应发送一封'
            f, t, raw = sent_mails[0]
            assert t == ['p@test.com']
            # MIME 编码（主题 RFC2047、正文 base64）→ 解码后断言
            import email
            from email.header import decode_header
            m = email.message_from_string(raw)
            subj = ''.join(p.decode(c or 'utf-8') if isinstance(p, bytes) else p
                           for p, c in decode_header(m['Subject']))
            body = m.get_payload(decode=True).decode('utf-8')
            subj_ok = '学习日报' in subj and TODAY[5:] in subj
            html_ok = '每日学习报告' in body and '听写' in body and '给家长的建议' in body
            print('✓ 发送成功：收件人/主题/HTML 内容正确', '(subj:', subj_ok, 'html:', html_ok, ')')
            assert subj_ok and html_ok

            db = sqlite3.connect(DB)
            db.row_factory = sqlite3.Row
            flag = db.execute("SELECT report_sent FROM daily_logs WHERE user_id=? AND date=?",
                              (uid, TODAY)).fetchone()['report_sent']
            db.close()
            assert flag == 1
            print('✓ report_sent 置位')

            out2 = report.send_daily_report(uid)
            assert out2['sent'] is False and '已发送' in out2['reason'], out2
            assert len(sent_mails) == 1, '幂等：不重发'
            print('✓ 二次调用幂等跳过')
    finally:
        os.remove(SMTP_CONF)
        db = sqlite3.connect(DB)
        db.execute("DELETE FROM quiz_log WHERE user_id=? AND date=?", (uid, TODAY))
        db.execute("UPDATE daily_logs SET report_sent=0 WHERE user_id=? AND date=?", (uid, TODAY))
        db.commit()
        db.close()
    print('✅ ALL PASS (report 发送路径单测)')


if __name__ == '__main__':
    main()
