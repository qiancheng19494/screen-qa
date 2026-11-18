# 屏幕问答 · Screen QA

> 圈一下屏幕上的题目，DeepSeek 自动读题、作答，并把答案自动复制到剪贴板。
> Windows 桌面小程序，Python + tkinter，无需安装 OCR 引擎（用系统自带 `Windows.Media.Ocr`）。

## 它是什么

一个贴在屏幕右下角的小悬浮窗：

1. 按 `Ctrl+Alt+Q` 框选题目区域（或 `Ctrl+Alt+W` 整屏、或直接 `Ctrl+V` 粘贴截图）；
2. 程序用 **本地 OCR / 视觉模型** 把图片读成文字（DeepSeek 官方 API 不接受图片，所以先读字再思考）；
3. 把文字交给 **DeepSeek** 多次独立作答 → 多数投票 → 交叉审核 → 置信度分级；
4. 答案显示在大字号卡片里，并**自动复制**到剪贴板；
5. 打开「连续截图」后，程序每隔几秒抓一次指定区域，**画面没变化就不调模型**（省钱），出现新题自动作答。

## 准确度是怎么提上去的（不是靠吹）

| 手段 | 作用 |
| --- | --- |
| 多次独立采样 + 多数投票 | 压掉模型偶发失误 |
| 分歧时自动追加 2 次采样 | 3 票不齐就再问 2 次，避免"2:1 侥幸通过" |
| 交叉审核（独立复核者角色） | 纠正"多数一致但都错"的系统性错误 |
| 高精度 OCR 多候选取优 | 原图 / 放大锐化 / 深色反色 三种候选按质量打分取最好 |
| OCR 误字自纠提示词 | 上标 ²、`sin`、标点被识别坏时由模型自行校正 |
| 置信度分级 | 🟢 高 / 🟡 中 / 🔴 存疑，不确定就直说，不假装确定 |

**必须说清楚**：没有任何程序能保证"错误率 ≤ 1%"。上面这些手段把错误率压到很低（同题三次采样一致 + 审核通过时最可靠），但模型本身、OCR、题目表述都可能出错。考试/重要场景请把它当"快速参考"，自己再核一遍。

## 安装

### 方式一：给 DeepSeek Harness（DSH）当技能装

```bash
npx -y skills add qiancheng19494/screen-qa --skill screen-qa --global
```

装完在 DSH 里就能用 `screen-qa` 技能（技能里写了完整调用步骤）。

### 方式二：直接跑（推荐给普通用户）

需要 Python 3.10+ 和 Pillow：

```bash
git clone https://github.com/qiancheng19494/screen-qa.git
cd screen-qa
pip install -r requirements.txt
python app.py
```

Windows 用户也可以克隆后直接双击 `启动.bat`（无控制台窗口）。

## 使用

| 操作 | 说明 |
| --- | --- |
| `Ctrl+Alt+Q` | 框选屏幕区域识别（拖框，Enter 整屏，Esc 取消） |
| `Ctrl+Alt+W` | 直接识别整个屏幕 |
| `Ctrl+V` | 粘贴剪贴板里的图片 |
| ▶ 连续截图 | 自动盯住一块区域，新题自动作答 |
| 📋 已自动复制 | 出答案即进剪贴板（可在设置里关） |

首次使用点右上角 ⚙ 填 **DeepSeek API Key**（[platform.deepseek.com](https://platform.deepseek.com) → API keys），也可以设环境变量 `DEEPSEEK_API_KEY`。

每次作答都会在 `history/` 下存一份 Markdown（题目原文 + 推理 + 答案），方便回看。

## 设置项

- **识别精度**：高精度（放大锐化 + 深色反色多候选取优，慢约 1 秒）／标准（更快）
- **采样次数**：1（省钱）/ 2 / 3（默认）/ 5（最稳）
- **交叉审核**：让模型换个角色复核答案
- **分歧时追加采样**：票数不齐时自动多问 2 次
- **出答案自动复制**：默认开
- **连续截图间隔**：默认 3 秒
- **模型**：`deepseek-chat`（默认，快）/ `deepseek-reasoner`（推理强，慢）

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `app.py` | 界面 + 主流程（`--cli` / `--image FILE` 无界面模式） |
| `capture.py` | 截图、DPI 适配、框选遮罩 |
| `psocr.py` + `psocr.ps1` | 调系统 OCR（`Windows.Media.Ocr`），含多候选增强 |
| `reader.py` | 识别层：视觉模型（可选 modlens）→ 本地 OCR 自动降级 |
| `solver.py` | 多采样 + 投票 + 追加采样 + 交叉审核 + 置信度 |
| `deepseek_api.py` | DeepSeek HTTP 客户端（纯标准库 urllib） |
| `config.py` | 配置读写与历史存档 |
| `hotkey.py` | 全局热键（RegisterHotKey） |

## 可选：接视觉大模型

装了 [`modlens`](https://github.com/liustack/modlens) 并把 `reader_order` 设为 `["modlens","winocr"]`，程序会先用视觉模型整图理解（公式、表格更准），失败自动降级到本地 OCR，降级原因会显示在日志里。

## 隐私

- 截图和识别文字都在本机处理，只有**识别出的文字**会发给 DeepSeek API 作答；
- API Key 只存在本地 `config.json`（已加入 `.gitignore`），不会上传；
- `history/` 里的记录也只在本机。

## License

MIT
