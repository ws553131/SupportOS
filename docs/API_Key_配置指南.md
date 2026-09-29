# API Key 配置指南

本文说明如何在 SupportOS 项目里安全地配置 API Key。**整个过程约 5 分钟。**

---

## 一、为什么必须用 .env，不能写在代码里

| 做法 | 后果 |
|---|---|
| ❌ 把 Key 写在 `config.py` 里 | 一旦 `git push`，Key 就永久留在 Git 历史里，**即使后来删掉也能被翻出来**。GitHub 上有爬虫 7×24 小时扫描泄露的 Key，几分钟内就会被盗刷 |
| ✅ 放在 `.env` 文件里 | `.env` 已在 `.gitignore` 中，永远不会被提交 |

**本项目的 `.gitignore` 已经配置好了**，`.env` 被明确排除。

---

## 二、第 1 步：申请 DeepSeek API Key

推荐 DeepSeek 的理由：便宜（约 ¥1/百万 token）、OpenAI SDK 兼容、中文效果好。**充 ¥50 够整个项目跑完。**

1. 打开 https://platform.deepseek.com/api_keys
2. 注册 / 登录（手机号即可）
3. 左侧菜单 **「API keys」** → 点击 **「创建 API key」**
4. 复制生成的 Key

> ⚠️ **Key 只会完整显示一次**，关掉页面就再也看不到了。请立刻粘贴到下一步的 `.env` 里。
> 如果不小心丢了，删掉重新创建一个即可。

5. 到 **「充值」** 页面充 ¥50（新账号通常有赠送额度，但建议充值以防中途断掉）

### 备选方案（可选，不必须）

| 厂商 | 申请地址 | 说明 |
|---|---|---|
| 通义千问 DashScope | https://bailian.console.aliyun.com/ | 有免费额度 |
| 智谱 GLM | https://open.bigmodel.cn/ | 有免费额度 |
| 月之暗面 Kimi | https://platform.moonshot.cn/ | 长上下文强 |

> **换厂商时只需改 `.env` 里的三行**（KEY / BASE_URL / MODEL），代码完全不用动——这就是用 OpenAI 兼容协议的好处。

---

## 三、第 2 步：创建 .env 文件

在项目根目录 `D:\ai产品\supportos` 执行：

```powershell
Copy-Item .env.example .env
```

然后用记事本打开：

```powershell
notepad .env
```

### 第 3 步：填入你的 Key

找到这一行：

```
SUPPORTOS_LLM_API_KEY=sk-你的deepseek密钥
```

把 `sk-你的deepseek密钥` 替换成你刚才复制的真实 Key，保存。改完应该长这样：

```
SUPPORTOS_LLM_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

> ⚠️ **上面是占位符，不是真 Key。** 这里刻意用 `xxxx` 而不是像真 Key 的字符，
> 避免被 GitHub 的密钥扫描误报，也避免读者误以为这是一把可用的密钥。
>
> ⚠️ **等号两边不要加空格**，Key 前后也不要加引号。

---

## 四、第 4 步：验证配置

```powershell
cd D:\ai产品\supportos
$env:PYTHONIOENCODING="utf-8"; $env:PYTHONUTF8="1"
python scripts\check_env.py
```

**配置成功的输出**：

```
[OK]  找到 .env 文件：D:\ai产品\supportos\.env
[OK]  LLM Provider : deepseek
[OK]  LLM Base URL : https://api.deepseek.com/v1
[OK]  LLM Model    : deepseek-chat
[OK]  API Key 已配置（sk-a1b...o5p6），长度 35

正在测试 API 连通性 ...
[OK]  API 调用成功，模型回复：'正常'
[OK]  Token 用量：prompt=8 completion=2
```

看到 `API 调用成功` 就说明**完全配置好了**。

---

## 五、关于 PowerShell 的中文乱码

Windows 的 PowerShell 默认用 GBK 编码，而项目里的中文输出是 UTF-8，会显示成乱码。**这不是程序出错。**

两个解决办法：

**办法 A：每次运行前设置环境变量（推荐）**

```powershell
$env:PYTHONIOENCODING="utf-8"; $env:PYTHONUTF8="1"
```

**办法 B：永久设置（一次搞定）**

```powershell
[Environment]::SetEnvironmentVariable("PYTHONUTF8","1","User")
```

设完重开 PowerShell 窗口即可。

---

## 六、重要：不是所有工作都需要 API Key

这是本项目一个刻意的设计——**Policy Engine 完全不依赖 LLM**，所以：

| 任务 | 需要 API Key 吗 |
|---|---|
| Policy Engine 单元测试 | ❌ **不需要** |
| Policy Engine 自查脚本 | ❌ **不需要** |
| 状态机测试 | ❌ **不需要** |
| Triage Agent（意图识别） | ✅ 需要 |
| Support Agent（信息收集） | ✅ 需要 |
| Action Planner（方案生成） | ✅ 需要 |
| 评测数据集跑批 | ✅ 需要 |

**所以你现在就可以先跑 Policy Engine 的测试**，不用等 Key：

```powershell
$env:PYTHONIOENCODING="utf-8"; $env:PYTHONUTF8="1"
python scripts\smoke_check.py
```

预期输出：

```
Policy Engine 自查结果： 42/42 通过
  全部通过 ✓
```

---

## 七、安全提醒

1. **不要把 `.env` 发给任何人**，包括截图时也要注意
2. **不要贴到聊天记录里**（包括发给 AI 助手）
3. 如果怀疑泄露 → 立刻到控制台**删除该 Key 并重新创建**
4. DeepSeek 控制台可以设置**消费限额**，建议设一个上限（比如 ¥100）防止意外超支
5. `.gitignore` 已经排除 `.env`，但**首次 `git add` 前建议先 `git status` 确认**里面没有 `.env`

---

## 八、常见问题排查

| 报错 | 原因 | 解决 |
|---|---|---|
| `SUPPORTOS_LLM_API_KEY 为空` | 没建 `.env` 或变量名拼错 | 检查 `.env` 是否存在，变量名是否完全一致 |
| `仍是模板占位符` | 忘了替换 | 把 `sk-你的deepseek密钥` 换成真实 Key |
| `AuthenticationError` | Key 无效或已删除 | 重新创建 Key |
| `InsufficientBalance` | 余额不足 | 去控制台充值 |
| `APIConnectionError` | 网络不通 | 检查网络/代理；确认 `base_url` 正确 |
| `model not found` | 模型名写错 | DeepSeek 用 `deepseek-chat` |
| 输出中文乱码 | GBK 编码 | 见第五节 |

---

## 九、一句话总结

```powershell
Copy-Item .env.example .env   # 1. 复制模板
notepad .env                  # 2. 粘贴真实 Key
python scripts\check_env.py   # 3. 验证
```

**三步完成。而 Policy Engine 的测试，你现在就可以跑，不需要等这一步。**
