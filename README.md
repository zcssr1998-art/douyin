# 抖音博主监控 · 艾丽的无废话财经

监控 [https://v.douyin.com/HRqWY-OM5PI/](https://v.douyin.com/HRqWY-OM5PI/)
（真实昵称：**艾丽的无废话财经**，抖音号 606890417，sec_uid `MS4wLjABAAAA_hkYGrUP3d2i3JodCLwrplan05tJ9xl1D6lZySbNC0Q`）

每 3 分钟检查一次新作品 → 下载音频 → 本地语音转文字 → DeepSeek 出观点分析 → 发到 Discord。

## 这条链路是怎么跑的

| 环节 | 文件 | 干什么 |
|---|---|---|
| 取作品列表 | `lib/fetch_feed.py` | 用无头 Chrome（后台静默浏览器）打开博主主页，从网页里抠出作品 ID 和标题 |
| 下载音频 | `lib/download.py` | 打开视频详情页，取出可直连的 mp4 地址，多线程无关的普通下载 |
| 语音转文字 | `lib/transcribe.py` | 本地 faster-whisper（small 模型，CPU），不联网、不上传 |
| 观点分析 | `lib/analyze.py` | 调本机 8769 端口的 DeepSeek 中转，出五段结构化分析 |
| 发 Discord | `lib/notify.py` | Webhook 或 Bot 发送，超 2000 字自动分段 |
| 串起来 + 记状态 | `lib/run_once.py` | 单次执行的状态机，每步落盘 `data/state.json` |

## 定时怎么跑的

Windows 计划任务 `DouyinMonitor-Ali`：

- 触发：登录时一次 + 之后每 3 分钟一次
- 动作：`run_once.bat`（用 `pythonw` 启动，不会弹黑窗）
- 同一时间只允许一个实例（`data/.run_once.lock`），上一轮没跑完时新的一轮直接退出

查状态：

```powershell
Get-ScheduledTaskInfo -TaskName "DouyinMonitor-Ali"
Get-Content "logs\run_once.log" -Tail 20 -Encoding utf8
```

临时停 / 恢复：

```powershell
Disable-ScheduledTask -TaskName "DouyinMonitor-Ali"   # 停
Enable-ScheduledTask  -TaskName "DouyinMonitor-Ali"   # 恢复
```

## 手动跑一轮（调试用）

```bash
python lib/run_once.py                 # 正常一轮
python lib/run_once.py --status        # 只看状态，不动手
python lib/run_once.py --baseline      # 重建去重基线（不会推送历史作品）
python lib/run_once.py --no-fetch      # 不拉列表，只推进没做完的作品
python lib/run_once.py --simulate-new <aweme_id>   # 把指定作品当新的跑一遍全链
```

## Discord 凭据（当前**未配置**，所以只落盘不发）

二选一，写进环境变量或 `config.json` 的 `discord` 段：

1. **Webhook（最省事）**：Discord 频道 → 编辑频道 → 整合 → Webhook → 复制 URL
   填 `DISCORD_WEBHOOK_URL` 或 `config.json` 的 `discord.webhook_url`
2. **Bot**：填 `DISCORD_BOT_TOKEN` + `config.json` 的 `discord.channel_id`

配好之后不需要改代码，下一轮自动把 `data/outbox/` 里攒下的成品发出去
（`stage=analyzed` 的作品会被重试，`stage=notified` 的不会再发第二次）。

## 去重与状态

- 新作品判定：**作品 ID 集合差集 + 发布时间窗**，不看列表位置（置顶视频 ID 很旧，位置判断必错）
- 发布时间由 ID 反推：`int(aweme_id) >> 32` 就是发布时的 unix 秒
- 比监控起点还早、只是这一轮才渲染出来的老卡片，会被标成 `baseline` 静默补录，**绝不推送**
- 处理阶段：`baseline → discovered → downloaded → transcribed → analyzed → notified`
- 每条最多重试 5 次，超了标 `failed` 停止自动重试（防无限重试打爆上游）
- 每步完成立刻原子写盘（`.tmp` + fsync + replace），进程被杀也丢不了状态

## 网络注意

- 抖音：css 只能用**家宽网卡**直连（默认出站走代理，抖音会 000），`download.py` 会自动尝试
- Discord：必须走 `http://127.0.0.1:7890`（`config.json` 的 `discord.proxy`）
- DeepSeek：本机 `http://127.0.0.1:8769/v1`，模型 `cline-pass/deepseek-v4.1-flash`

## 不要去动的东西

- `data/state.json` 是唯一的事实来源，手工改之前先备份
- `data/chrome-profile` 是浏览器配置目录，两个进程同时用会互相污染（已加锁，别绕过）
- 三个子模块的 CLI 契约（stdout 末行单行 JSON）是 `run_once.py` 依赖的，改输出格式要同步改调用方


## 目录结构

```
douyin-monitor/
├─ config.example.json   配置模板（复制成 config.json 再改）
├─ config.json           真实配置（含凭据，已被 .gitignore 排除，不在仓库里）
├─ run_once.bat          计划任务的入口（纯 ASCII，靠 %LOCALAPPDATA% 找 pythonw）
├─ lib/
│  ├─ fetch_feed.py      取作品列表（无头 Chrome 渲染主页）
│  ├─ download.py        下载视频并抽出音轨
│  ├─ transcribe.py      本地语音转文字
│  ├─ analyze.py         调 DeepSeek 出五段分析
│  ├─ notify.py          发 Discord（Webhook 或 Bot，>2000 字自动分段）
│  └─ run_once.py        串起全链 + 状态机（唯一入口）
├─ data/                 运行态，不入库：state.json / media / transcripts / analyses / outbox / chrome-profile
└─ logs/                 日志，不入库
```

## 仓库里为什么没有 config.json 和 data/

- `config.json` 会放 Discord Webhook 或 Bot Token，还有本机绝对路径
- `data/` 里是作品清单、逐字稿、分析结果，以及几百 MB 的浏览器配置目录（`data/chrome-profile`）

两者都在 `.gitignore` 里。克隆下来第一步：

```bash
cp config.example.json config.json   # 然后按注释填 model_dir / analyze.model / discord
```

## 在另一台机器上部署

1. 装 Python 3.14（安装时勾 Add to PATH），再装依赖：
   `pip install faster-whisper ctranslate2 av requests`
2. 装 Chrome（无头渲染主页要用）
3. 下载 faster-whisper 的 small 模型（CTranslate2 格式，含 `model.bin` / `config.json` / `tokenizer.json`），目录填进 `asr.model_dir`
4. 准备一个 OpenAI 兼容的 DeepSeek 接口，地址填 `analyze.base_url`、模型 id 填 `analyze.model`
5. 复制并填写 `config.json`（见上一节）
6. 先手动验证：
   ```bash
   python lib/run_once.py --status     # 只看状态
   python lib/run_once.py --baseline   # 建去重基线，不推历史作品
   python lib/run_once.py              # 真跑一轮
   ```
7. 注册计划任务（管理员 PowerShell）：

```powershell
schtasks /Create /TN "DouyinMonitor-Ali" /SC MINUTE /MO 3 /TR "\"<项目路径>\run_once.bat\"" /F
```

   这条只做"每 3 分钟"；要"开机登录也跑一次"，在任务计划程序界面里给同一个任务再加一个"登录时"触发器即可。

## 已知限制

- 抖音网页是动态渲染 + 需要签名，列表靠后台 Chrome 从渲染后的 DOM 里抠，所以**必须有 Chrome**，且每轮约 5~8 秒
- 抖音的 CDN 在默认代理出口下会拒绝连接（返回 000），需要走家宽网卡；`download.bind_source_address` 留空表示自动探测
- 没有 ffmpeg 也能跑：音频解码走 PyAV
- 真实"新视频发布 → 收到推送"的延迟取决于博主的发布节奏，只能等真实发布验证，模拟测试不代表线上延迟

