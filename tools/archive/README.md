# 归档：内容校对工作台（2026-11-16）

`review.html` + `review_data.js`——v4.2 时期的义项人工终审台（README v4.2 §4）。
使命已完成：低置信义 202 → 2（2026-11-16 查库），遂移出 `app/static/`（不再公网可达、
镜像减 1MB 死重）。

重启方法（若需校对新增自选词库的 low 义）：

```bash
cd data/parsed && python3 -I gen_review_page.py   # 直接生成 app/static/review_data.js
git mv tools/archive/review.html app/static/      # 页面移回即可用
```

注意 review.html 内引用 `/review_data.js`（绝对路径），两文件需同在 `app/static/` 才可用。
