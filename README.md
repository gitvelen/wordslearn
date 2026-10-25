# WordsLearn 单词星球

面向自家两个孩子的上海牛津版英语单词学习应用。

## 架构

- **后端**: FastAPI + SQLite（`app/main.py`），容器内监听 8000，仅暴露于 `main_default` docker 网络
- **前端**: Vue3 自托管单文件（无构建链），`app/static/`
- **反代**: 复用宿主机 Caddy（main-caddy-1），子域 `words.velen.ltd`
- **词表管线**: `fetch_vocab.py`（两阶段：download 快照落袋 → parse 结构化）

## 核心设计（2026-10-24 v2 定稿，勿随意改动）

1. **调度状态机**: 首学 stage=1,next=D+2 → 答对 stage=2,next=D+5 → 答对 stage=3,next=D+12 → 答对 mastered。**复习答错: stage 重置为1, next=D+2 完整回炉**；前端把错词推回当日队尾重现（**重现轮不提交API**——30秒后的答对不作数，回炉轨迹完整）。单日复习上限 REVIEW_CAP=40（积压分日消化，不改调度状态）。today 接口附 distract_pool 随机释义（防小队列时选项枯竭）。首学幂等（重复提交不双计 new_done）。
2. **新词投放**: 在 `end_date - 12天` 停投（REST_DAYS=12），尾部纯复习收尾。daily_new = target/(days-12)。计划天数下限 14。
3. **欢迎引导流**（首次登录，users.grade 为空时触发）: 选年级（1A-9B/adult，自动推导 junior/standard 模式）→ **目标意向三档（词数）+ 时间选择（30/60/90天）** → 水平小测说明屏（可跳过，skip 落库）。意向词数+天数共同作为 setup 默认值。
4. **年级水平抽查（spot v2，2026-10-24）**: 3年级及以上用户，摸底前先做 80 题随机四选一（P13/P45 各40，同词性干扰，约5分钟）。**词级答案提交，后端自算段级判定**：正确率≥92.5% 的段整段赦免（skipped）→ 计划池直接从初中段开始；但**小测答错的词标 new 捞回**（有直接证据的漏洞，进计划池前排，不随段赦免）；未达标段进入该段的全量细致摸底。防重复提交、答案完整性校验（40/段）。1-2年级/adult 无抽查。
5. **细致摸底**（仅未达标段）: 自评三档（认识→待验证/模糊、不认识→学习池）→ "认识"的做四选一验证，**连续两次答对才剔除**；复验与首验之间至少隔一批自评（**设计边界**：最后一批自评的首验词无自评可插，复验紧随其后但间隔≥20道其他验证题，靠题目干扰保证记忆衰减）。批次 50 词。
4. **干扰项同词性优先**（前后端皆是）：防孩子靠词性排除法猜对。
5. **计划词汇池**: P13 → P45 → JUNIOR，段内按 COCA/wordfreq 词频排序（高频先行，`fetch_vocab.py frq` 生成 + `seed.py frq` 导入）。
6. **emoji 视觉锚点**: 仅高置信映射（手工词典174词 + Unicode官方名精确匹配 + 颜色修饰后缀），国旗/俚语/模糊匹配一律不用——质量优先于覆盖（281词）。
7. **双模式同引擎**: standard（五年级）/ junior（一年级，音频驱动、隐藏音标、大卡片、中文释义自动朗读）。

## v2.1 教学流程改版（2026-10-25：一次性呈现 → 分关测验）

1. **每日结构**: 热身轮 → **新词卡片连播**（纯呈现：发音/义项/例句/跟读，无测验，learn 提交在卡片完成时）→ **复习组**（沉淀间隔，洗掉工作记忆残留）→ **分关测验**（题型分批、各关词序独立打乱）→ 完成。
2. **五关**（standard）: 认词(word_zh)/听音(listen_zh)/回想(zh_word)/听写(spell)/用词(reinforce·挖空)；**三关**（junior）: 认词/听音/选词(emoji 优先退化 zh_word)。通道难度与间隔错位匹配：最易的再认配最长间隔，最难的听写配最短间隔。
3. **答对自动过（900ms，听写先重播单词音）；答错弹完整词条卡**（按钮 1s 强制注视期防秒点）→ 点「记住了，继续」直接推进（两次点击合并为一次）。机械点击从 ~135 次/天降到 ~35 次。
4. **关末结算**: `N/M` 成绩 + 错词同题型重测（测到对为止，`gateRetry` 用原题重建 splice 重插）；quiz 不进调度（首日不考试哲学）。
5. 工程: buildQueue 重构（newcard/quiz/gate 三队列类型）、advance 统一推进、修 curItem 空引用与 todayPhaseDone 恒真两个历史 bug。后端零改动。

## v4.2 增量（2026-10-25：新词多角度学习 + 词典式义项完备）

1. **新词首学 4 题（多角度）**: word_zh(形→义) + listen_zh(音→义) + zh_word(义→形) + **spell(音→形·当天听写)**，全部不计调度；junior 为 3 题（word_zh + listen_zh + emoji 听音选词，无 emoji 词退化 zh_word），新词卡附 🎤 跟读（录音回放自比，无 ASR 打分）。`get_learn_quizzes` 按题型序列取题（`_quiz_by_type`）。
2. **巩固轮**: 当日新词全部学完后同会话自动追加，每词 1 题从 d5/d2 池借用（`mark_used=False` 不消耗正式复习防重复配额），前端 type='reinforce'，答错重现不进调度。
3. **答错看卡**: 复习首答错 → `/api/word_detail` 弹层展示全部义项+例句+词形，"记住了，继续"才放行（内容曝光恰在最需要时）。
4. **义项词典式完备**: word_senses 2878→3558 义（多义词 443→907），每义双语释义+例句+confidence（低置信 202 词进 review.html 校对台人工终审）。管线三件套：SENSES_V2_SPEC.md 规格 → 17 分片生成 → validate_senses_v2.py（强词形归一：不规则/去e/y变i/双写/fe变ves/分隔短语/可变成分）→ final_audit.py（A 例句含词 0 / B 超纲 0，ECDICT frq + BNC lemma.en.txt 兜底）。语域剔除口径（用户拍板）：酒/武器/赌博/毒品/性/暴力细节/政治义项不收。
5. **词形变化表**: ECDICT exchange → `gen_forms.py`（按全部义项词性过滤，even 纯副词不收 evened）→ words.forms JSON（1583 词），首学卡/弹层展示。
6. **gen_quiz v4.2**: learn 4 题 + learn/emoji（281 词）；干扰项新增**同义排除**（核心义与目标词相同的词不作干扰，防四选一双正确）；题库 26711 题。
7. **streak/日历修复**: 纯复习日（收尾期）streak 不断（review_done 计入）；日历收尾期复习达标着绿色。
8. **测试**: tests/e2e.py（v4.2 重写，25 断言全绿：含状态机全走查 D+2→D+5→D+12→mastered+回炉+巩固题约束）+ tests/smoke-v42.js（浏览器冒烟 39 断言全绿：4 题链/巩固轮/答错弹层/junior emoji+跟读）。旧 invariant-test.js 为 v4.0 版已失效待重建。

## 数据源与已知局限

- 小学: koolearn 上海牛津词表（**粗分级**: P13=1-3年级段 728词, P45=4-5年级段），详情页含音标/中英释义/英英/例句/真人mp3
- 初中: 上海市初中英语词汇表 2020 官方 PDF（2101词条，无音标——待 ECDICT 补齐）
- 局限: ① 无逐册(1A/1B…)分级，只有 P13/P45/JUNIOR 三段; ② 新概念一册词表未接入（摸底池待扩展）; ③ 初中词无音标无mp3（回退浏览器 TTS）

## 测试体系（2026-10-24 重建：会话不变式）

**失效教训**：此前的"单点存在性断言 + API 状态机断言"抓不到"状态组合/时序"类 bug（用户实测抓出4个：分段成绩缺失、进度口径、文案阶段脱节、背靠背重验）。重建为三层：

1. **e2e.py**（容器内，~50项）：API 状态机与数据层断言
2. **browser-test.js / browser-test-done.js**：关键路径 UI 冒烟
3. **invariant-test.js（核心）**：会话不变式——三个用户画像（mily型混合/mido型junior/velen型家长）完整连续走流程，**每步交互后**断言 10 组不变式（I1阶段-文案一致/I2引用完整/I3进度语义==pending/I4同词不背靠背/I5分支后下一屏匹配/I6四选一答案存在且唯一/I7按钮-状态一致/I8刷新恢复/I9无重复渲染/I10结果屏分段成绩），结束时 D 类数据库审计（状态组合合法/计划无重复/日志==首学数/无悬空verify）。行为随机化可 seed 复现（SEED 环境变量）。

```bash
cd /tmp/wltest && node invariant-test.js          # ~12分钟（画像A含全量摸底）
SEED=7 node invariant-test.js                     # 换随机种子再 fuzz 一轮
```

## 运维

```bash
# 数据导入（幂等，可重复执行）
docker compose -f /root/wordslearn/docker-compose.yml exec wordslearn python seed.py words
docker compose -f /root/wordslearn/docker-compose.yml exec wordslearn python seed.py users
docker compose -f /root/wordslearn/docker-compose.yml exec wordslearn python seed.py audio

# 重启 / 日志
docker compose -f /root/wordslearn/docker-compose.yml restart
docker logs -f wordslearn

# 数据备份（SQLite WAL 模式，热备用 .backup）
docker exec wordslearn python -c "import sqlite3; sqlite3.connect('/app/data/wordslearn.db').execute(\"VACUUM INTO '/app/data/backup.db'\")"
```

## P1 待办

- 例句：Tatoeba API 已死（404），备选方案待评估（Tatoeba 数据包 ~600MB 或 LLM 批量生成或 koolearn 双语区补抓）——当前例句不可用（成人词典句）
- ECDICT 完整包补初中词音标（76MB zip 在 ghproxy 断流，需换源）
- 新概念一册词表接入摸底池
- 逐册精校分级（P13/P45 → 1A..5B）
- 拼写练习课、家长周报、错词本重练
- stage 3 毕业关改遮挡回忆式（等两周真实使用数据再定）
