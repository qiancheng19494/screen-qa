## 十二、v2.7：验算 / 花费 / 回归 / 追问 / 区域 / 中英（13 项）

这一轮不是"多加按钮"，而是把"答案对不对、花了多少钱、以后会不会退化"变成可看、可算、可回归的东西。

| 能力 | 代码位置 | 关键取舍 |
|---|---|---|
| 代入验算 | `verify.py` + `solver.py` 第 6 步 | 不依赖模型自证：把答案代回原式用纯 Python 求值（`_SafeEval`，白名单函数、空 builtins）；`ok / fail / skip` 三态，拿不准就 skip，绝不错判 pass；`fail` 只降置信度不篡改答案 |
| OCR 变体容错 | `verify.verify()` 多策略 | 真实 OCR 会把 `x` 认成 `×`、把 `x²` 压平成 `x2`，所以对 4 个变体各试一遍：任一成立即 ok；只有默认变体确实 fail 才报 fail |
| 花费记账 | `deepseek_api.usage_total` + `config.cost_of` | 从响应 `usage` 累加 prompt/completion token，按 `price_in=2.0 / price_out=8.0` 元每百万计，写入 `records.jsonl`，答案卡显示 `¥0.0059` |
| 余额查询 | `deepseek_api.balance()` | 打官方 `/user/balance`，返回可用余额；设置里「余额」按钮 + 启动静默查一次，顺带估算"还能做 N 题" |
| 回归套件 | `tools/regress.py` + `tests/cases.json` | 8 条已知答案用例，一条命令出正确率/均耗时/总花费；改代码先跑它，别靠感觉 |
| 追问 | `run_followup()` + `Ctrl+Alt+F` | 单次调用、不投票不缓存；适合"第二问""为什么"这种不需要三票的场景 |
| 数字核对 | `ocrcheck.audit()` | 只提示（上标压平 / 答案数字没出现在推理里），不改结论，避免误伤 |
| 前后一致性 | `App._note_answer_consistency` | 同题号两次答案不同就提醒，靠 `Question N of M` 解析，宁可不报也不硬匹配 |
| Anki 卡片 | `config.export_anki()` | 错题本 → TSV（utf-8-sig），列 = 题面 / 答案(标记 N 次)，直接导入 Anki |
| 多区域记忆 | `config.regions` + `region_menu()` | 最多 5 个区域，同名覆盖；切区域只改采集矩形，不改其它设置 |
| 卡死看门狗 | `App._watch()` | 只看"忙了多久"（默认 120s），不强行杀线程，避免把正在写的缓存搞坏 |
| 中英界面 | `prompts.py`（`LBL` / `set_lang()` / `L()`） | 只翻界面标签与提示，模型提示词保持中文（中文提示对数学题更稳） |
| 强调色跟随 | `App.apply_system_accent()` | 读注册表 `DWM\AccentColor`（ABGR）转 `#RRGGBB`；失败就保持默认蓝 |
| 下载更新 | `extras.prepare_update()` | 只下到 `_update/` 不覆盖运行中的文件；解压用内存 tar 解包去掉顶层目录 |
| 绿色版 | `tools/make_portable_zip.py` | onedir 产物直接压 zip，发人即可用，无需安装 |
