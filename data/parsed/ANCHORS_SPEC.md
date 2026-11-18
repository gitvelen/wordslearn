# ANCHORS_SPEC — 音节切分 + 搭配锚 数据规格（v4.3）

产出两份词级数据（`gen_out/word_anchors.jsonl` → `seed.py anchors` → `words.syllables` / `words.collocations`），用于首学 card、答错弹层、spell 补测揭示态的展示。**展示端 v-if 数据驱动：NULL 即不显示。**

## 范围与排除

- 全库 2447 词一次生成；**短语词（含空格/连字符，如 make a decision、ice-cream）一律不生成**（切分+搭配都跳过，进 PHRASE 清单）——短语本身即搭配锚，且短语音节切分无教学规范。
- 单音节词（n=1）不生成切分（无可切分），搭配仍生成。
- 音节数两源不一致或双缺 → 不生成，进 MANUAL 清单。

## 音节数 n 的确定（双源交叉，L1）

- **主锚：cmudict ARPAbet 元音计数**——`ARP_VOWELS`（pipeline.py:177）固定 15 符号，每符号=1 音节核；双元音 AW/AY/EY/OW/OY 为单符号天然 1 节；ER=1 节（r-controlled）。无解析歧义。
- **交叉源：phonetic_us（缺则 uk）IPA 解析**——剥 `ˈˌː().`；元音字符集 + 双元音合并表 `aɪ aʊ eɪ ɔɪ oʊ əʊ ɪə eə ʊə`（左到右贪心）；ɚ/ɜ 各 1。
- 两源一致 → n 生效；否则 MANUAL。

## 切分规则（v2 权威重构，2026-11-18）

**方法论更正**：初版用"phonics 规则文本 + LLM 双跑"生成切分，用户指出必须以权威词典为准。实测证实分歧真实存在（cinema：LLM 依最大节首给 ci·ne·ma，朗文/韦氏均为 cin·e·ma；ruler：LLM ru·ler，朗文 rul·er）——权威切分是**发音音节 + 形态学边界优先**（词素边界优先于音系最大节首），自拟规则无法可靠复现。

### 权威数据链（切分位置的依据）

1. **主锚：Longman Dictionary of Contemporary English（ldoceonline.com）headword 切分**——fetch_ldoce.py 批量抓取（0.6s/词礼貌速率），如 op‧por‧tu‧ni‧ty、cin‧e‧ma、rul‧er。单音节词 LDOCE 不显示切分（与"单音节不切分"设计天然一致）。
2. **仲裁抽查：Merriam-Webster（merriam-webster.com）**——cin·e·ma 与 LDOCE 交叉一致；反爬限制仅抽查用。
3. 硬校验不变：LDOCE 切分拼接必须逐字符还原原词；块数必须 == cmudict 音节数（LDOCE 与 cmudict 不一致的词标 SUSPECT 进人工清单——防抓取错误）。
4. **LLM 双跑切分降级为补洞**：仅 LDOCE 无词条的词使用，review.csv 中标注来源 LLM_FALLBACK。

### 人工核验清单构成（v2）

- LDOCE vs LLM 分歧词全量（已被 LDOCE 替换，供确认替换正确）
- SUSPECT 全量（LDOCE 与 cmudict 音节数不一致）
- LLM_FALLBACK 全量（LDOCE 缺词、由双跑切分顶上）
- DROPPED/PHRASE/MANUAL 存量 + AGREE 随机抽检

## 附：初版 phonics 规则文本（保留作 LLM 补洞的 prompt 依据）

1. 形态学优先：前缀 un-/re-/dis-/pre-/per-/con-/com-/op-/sub- 等成块；后缀 -tion/-sion/-ic/-al/-ly/-er/-est/-ed/-ing/-ment/-ness/-ful/-less 整块不拆。
2. 二合字母 sh/ch/th/ph/wh/qu/ck/ng 不拆；双写辅音从中间切（let·ter）。
3. 单辅音归后节（ti·ger）；辅音簇能做节首则整体归后（bas·ket）。
4. 辅音 + -le 结尾成节（ta·ble）。
5. r-controlled 元音与 magic-e 不拆。
6. 硬约束：块数 == n；拼接逐字符还原原词；无空块。

## 搭配规格与校验

- 结构 `[{"en":"make a decision","zh":"做决定"}]`，1-2 条；具体名词/自解释词输出 null。
- 校验（不过即弃该条，全不过 → collocations=NULL）：
  - en 含目标词原形或规则变形（_lemma_forms 形态集，短语成分间允许插入）；
  - en ≤6 个英文词，全部成分词（除目标词）∈ 主库词表 ∪ eng_50k 前 5000（超纲即弃——锚定材料不加新负担）；
  - zh ≤10 字；条目去重；与义1 example_en 不同；
  - 必须匹配义1（prompt 注入义1 pos+definition_zh）；
  - **双跑交集投票**：两跑 en 完全一致（小写化）的条目才收——等效地道性双盲投票，零额外调用。

## 产物

- `gen_out/anchors_run1.jsonl` / `anchors_run2.jsonl`：双跑原始（key=word，断点续跑）
- `gen_out/word_anchors.jsonl`（定稿）：`{"word","syllables","collocations"}`（任一可 null）
- `gen_out/anchors_stats.json`：L1 分布 / 双跑一致率 / judge 仲裁数 / DROP 数 / 搭配非空数
- `gen_out/anchors_review.csv`：人工核验清单

## 量化门禁

- 双跑一致率 ≥80%（低于先修 prompt 再继续）；DROP 率 ≤10%（高于则核对 n 规则）。
- 覆盖率服从正确率。
