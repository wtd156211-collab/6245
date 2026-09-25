# wt-037 viewkeep 物化视图增量维护（从 0 实现）

起始环境只有说明与素材（见第 4 节），`web/` 空着，引擎与页面从零写。Python 3.13、只用标准库；页面是原生
HTML/CSS/ES module，无构建、无依赖。

## 1. 范围

做的：维护 `views.json` 定义的物化视图（按 `samples/changes/` 的 3000 步变更**增量**）；提供读全量与按键读分组两个接口；
导出 `web/data.json`；用原生 ES module 渲染页面。

视图只支持：单表或「主表 + 一张维表」的等值内连接；`filter` 为 AND 组合的谓词（`=`、`!=`、`<`、`<=`、`>`、`>=`、`in`、
`not_in`）；`group_by` 至少一列；聚合只有 count/sum/min/max；输出只有分组键列与聚合列（投影 = 列裁剪）。

不做：嵌套视图、窗口函数、HAVING、排序/LIMIT/DISTINCT/UNION/子查询、外连接、自连接、三张及以上表、无分组的整表聚合、
非等值连接、数据库、并发、鉴权、前端框架与构建工具、第三方依赖。

## 2. 口径与公式

- 视图内容 = 对**当前**源表做「连接 → 过滤 → 分组 → 聚合」，任何时刻都成立。分组键 = `group_by` 各列值的元组；行 =
  `{"key":[...],"values":{...}}`，`values` 按聚合定义顺序，行按 `key` 逐列升序（字符串比码点）。`count` 数行数；`sum` 求和；
  `min`/`max` 取最值（`int` 比数值、`str` 比码点）；`int` 列不出现浮点。
- 空分组：分组没有满足过滤与连接条件的行时**从视图内容里消失**，不留 `count=0` 零值行；之后再有行落进去时按新数据重建。
- 传播：插入/更新/删除都修到与全量重算一致——只改未引用列（如 `customers.city`）不变；更新把行挪到别的分组时旧分组减、
  新分组加；删除可能让分组消失；维表行被删时连接失配，行只从连接视图退出。
- 一步可带多个操作，按数组顺序作用于源表，视图只在该步结束时可见；该步变化 = 前后视图内容之差，同一行同一步里改多次只按
  净效果算一次。`update`/`delete` 的主键必须存在、`insert` 的必须不存在、`set` 不能含主键，否则整步失败（退出码非 0）。

## 3. 状态机与数据结构

- 水位 `watermark`：`build` 后为 0，每成功应用一步加一；视图内容永远对应水位那一刻。
- 幂等与跳号：`step <= watermark` 跳过、`step > watermark+1` 整体失败——同一批变更跑两遍结果完全一样。
- 日志 `var/log/applied.jsonl`：每步追加 `{"watermark":步号,"ops":操作条数,"changed_rows":{"视图名":该步变化行数}}`，只列有变化
  的视图。回收：只留最近 200 步，`watermark>200` 时首行必须是 `watermark-199`，跑完 3000 步后恰好 200 行、与
  `expected/log_tail.jsonl` 一致（内存同窗口回收）。
- 持久化：`var/state.json` 结构自定，但要能重启后续跑、`dump`/`query` 结果不变；`web/data.json` 只在 `build`/`apply` 结束时
  各写一次，逐步明细另行落盘。

## 4. 输入输出与文件格式

素材 UTF-8、LF、末尾换行。`samples/init/`：`schema.json`（列名、类型、单列字符串主键）、`tables.json`
（`{"orders":[...],"customers":[...]}`，行顺序无意义）、`views.json`（4 个视图定义）；`samples/changes/part-01..06.jsonl`
每行一步 `{"step":n,"ops":[...]}`，500 步一个文件，按文件名升序拼起来是 1..3000；`samples/notes.md` 非规格。

`samples/expected/` 四份（比对按解析后的 JSON 值，空白与键顺序不计，字段不能多不能少，数组顺序要一致）：

- `deltas.jsonl`：每步 `{"step","views"}`，每个视图是 `{"inserted","updated","deleted"}`——inserted/deleted 为整行
  `{"key","values"}`，updated 为 `{"key","from","to"}`；无变化的视图不出现，三类各自按 key 升序。
- `snapshots/step-XXXXXX.json`：检查点（0、250、…、3000）的全量 `{"step","views":[{"view","rows","vanished"}]}`，`vanished` 项是
  `{"key","appeared_step","vanished_step","last_values"}`；`queries.jsonl` 是 `{"step","view","key","row"}`（查不到为 `null`）；
  `log_tail.jsonl` 是水位 2801..3000 的日志行。

命令行（`app.py` 是唯一入口）：

```
python app.py build
python app.py apply samples/changes/part-01.jsonl ...
python app.py dump [--view 名字]
python app.py query --view 名字 --key k1[,k2]
python app.py serve [--port 8000]
```

`build` 读 `samples/init` 建视图（`watermark=0`），写 `var/` 与 `web/data.json`；`apply` 续跑、结束时写 `web/data.json`。
`apply` 结束在 stdout 打一行 `{"watermark":3000,"applied":3000,"skipped":0}`（重复跑时 `applied=0`）；`dump`/`query` 打的就是
上面同名期望对象的形状（`step` 换成当前水位），键长必须等于分组列数，未知视图或非法键退出码 2。

`web/data.json`：`watermark`、`views`（= 最后一个检查点快照加 `key_columns`/`value_columns`）、`checkpoints`（= 全部检查点快照）、
`steps`（= deltas 各行加该步 `ops`）；三者分别对上最后一个快照、各检查点快照、`deltas.jsonl`。

页面必须体现：① 每个分组一张卡片，键与聚合值直接取自 `data.json`（页面不自己聚合、不读 `samples/`）；② 已消失的分组保留
卡片并置灰，标出出现/消失步与最后一次的值；③ 可选 0..watermark 的步号，显示该步 `ops` 与该步各视图的新增/更新/删除行，
选到检查点时卡片墙换成该检查点全量；④ 顶部显示当前步号与 watermark。`python app.py serve --port 8000` 后开
`http://localhost:8000/`，标准库服务把 `web/` 当静态目录，页面 `fetch("./data.json")` 取数。

## 5. 性能与验收口径

规模：初始 `orders` 1200 行、`customers` 240 行；3000 步变更（插入 1006、更新 2034、删除 670）；终态四个视图 217/9/8/5 组。
开销要跟**变更量**相关：单步只许碰这一步涉及的分组（通常 1~3 个）与其中成员行，不许每步全量重算或扫全表。
测试机预算：`build` ≤ 2 秒；3000 步 `apply` ≤ 3 秒（单步峰值 ≤ 50 ms）；一次按键查询 ≤ 1 ms；峰值内存 ≤ 256 MB。对照：每步
全量重算约 16 秒，过不了预算。

验收：① 逐步对拍 `expected/deltas.jsonl`、各检查点快照、`expected/queries.jsonl`；② 空分组消失不留零值行、重现重建；③ 同一批
跑两遍逐字一样，分两次 `apply` 与一次跑完一致；④ 跑完 `var/log/applied.jsonl` 恰好 200 行、与 `expected/log_tail.jsonl` 一致；
⑤ 页面四项信息齐全、数字来自 `data.json`；⑥ 自测只读 `samples/`，用 `unittest` 写。

## 6. 样例说明

4 个视图（`views.json`）：3 个单表分组（过滤分别是 `status in`、`region !=`、无过滤）+ 1 个 orders 连接 customers 后分组
（过滤 `tier !=`）；聚合覆盖 count/sum/min/max。

3000 步里 289 步带 2~4 个操作、266 步无视图变化，覆盖：插入/更新/删除与跨分组更新（改 `customer_id`/`region`/`channel`/
`status`）；搬空分组再重建；维表 `tier` 变更让整批订单换组；维表行删除与插回（连接失配）；同一步内更新两次抵消、插入后立刻
删除；只改未引用列、只改 `updated_at`、把值设回原值；跨表多操作；末尾客户流失（12 个分组永久消失）。

`expected`：3000 行逐步变化 + 13 个检查点全量 + 3620 次按键查询（363 次查不到 → `null`）+ 最近 200 步日志；任一步都能用
「上一个检查点 + 其后 delta 依次叠加」复算。

## 7. 待补的文档

视图语法的报错码与文案、`var/state.json` 的字段设计、日志窗口取 200 的依据、页面配色与分页、多进程约定、放大数据怎么生成，
都还没定。
