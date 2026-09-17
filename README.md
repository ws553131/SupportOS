# SupportOS

> **AI 原生客服运营后台** —— 让 AI 从「帮客服写回复」，升级为「在规则约束下安全处理客服业务」。

**核心命题**：AI 可以执行真实业务动作，但必须受到**权限、风险规则、人工审批和审计体系**的约束。

---

## 这个项目要证明什么

| # | 命题 | 当前状态 |
|---|---|---|
| 1 | AI 能否正确理解并解决客服问题 | 🔄 进行中（W2–W3） |
| 2 | AI 能否安全调用真实业务 Tool | 🔄 进行中（W2–W3） |
| 3 | **当 AI 不应该行动时，系统能否阻止它** | ✅ **W1 已完成** |

---

## 核心设计：LLM 决定「想做什么」，规则系统决定「能不能做」

```
LLM 建议：      退款 ¥399
                    ↓
Policy Engine： refund, amount=¥399 → risk=HIGH → action=ESCALATE
                    ↓
结果：           Agent 不执行，进入人工审批
```

关键原则：

1. **Agent Permission ≤ Human Permission** —— `User Permission ∩ Agent Policy ∩ Risk Policy`
2. **AI 不允许审批自己的动作** —— `Proposal Agent ≠ Approval Authority`
3. **不依赖 LLM 自报置信度** —— 只用可观测信号（政策命中数、分类分歧、采样一致性、Tool 失败）
4. **不确定时优先升级** —— 宁可少自动化，不可越权

---

## 快速开始

### 1. 运行 Policy Engine 测试（**不需要 API Key**）

```powershell
cd D:\ai产品\supportos
$env:PYTHONIOENCODING="utf-8"; $env:PYTHONUTF8="1"

python scripts\run_tests.py       # 43 个单元测试
python scripts\smoke_check.py     # 42 项决策逻辑自查
```

预期输出：

```
结果： 43 通过 / 0 失败 / 共 43
  全部通过
```

> Policy Engine 刻意**不依赖任何 LLM**：裁决必须可复现、可测试、可审计。
> 因此这部分工作完全不需要 API Key。

### 2. 配置 API Key（做 Agent 层时才需要）

见 [`docs/API_Key_配置指南.md`](docs/API_Key_配置指南.md)

```powershell
Copy-Item .env.example .env
notepad .env                              # 填入真实 Key
python scripts\check_env.py               # 验证连通性
```

---

## 目录结构

```
supportos/
├── src/supportos/
│   ├── constants.py            # ★ 所有阈值集中于此（改阈值不碰业务逻辑）
│   ├── config.py               # 环境变量与 LLM 客户端
│   ├── domain/
│   │   ├── enums.py            # Intent / RiskLevel / Decision / TicketState
│   │   └── models.py           # Customer / Order / CaseContext / PolicyDecision
│   ├── policy/                 # ★★★ 产品灵魂
│   │   ├── engine.py           # Policy Engine（数据驱动，非 if-else 面条）
│   │   └── permissions.py      # Agent Permission ≤ Human Permission
│   ├── tools/                  # 6 个 Tool（4 读 2 写）
│   ├── agents/                 # Triage / Support / Action Planner
│   ├── orchestrator/           # 9 状态机 + 主编排
│   ├── audit/                  # 全链路审计日志
│   ├── evals/                  # 评测执行器与指标
│   └── web/                    # 4 个页面（W5 才做）
├── tests/                      # 单元测试
├── scripts/                    # 自查与工具脚本
└── docs/                       # 配置指南等
```

---

## Policy Engine 的架构选择：为什么数据驱动

规则**不写成 if-else 面条**，而是用 `ActionSpec` 声明：

```python
ActionType.ISSUE_COUPON: ActionSpec(
    risk=RiskLevel.YELLOW,
    deny_if=(_p_amount_positive, _p_over_user_coupon_limit, _p_delay_below_sla),
    escalate_if=(_p_coupon_frequency_exceeded, _p_daily_coupon_total_exceeded),
    approve_if=(_p_over_agent_coupon_limit,),
    require_policy=True,
)
```

好处：**新增动作 = 加一条数据，不改引擎逻辑**；每条规则可独立测试、可审计追溯。

### 裁决优先级（安全优先）

```
0. 自审批检查      → 拒绝
1. 明确禁止动作    → 拒绝
2. 前置信息完整性  → 升级
3. 知识缺口        → 升级（禁止编造规则）
4. deny_if         → 拒绝
5. escalate_if     → 升级
6. 可观测升级信号  → 升级（仅对写操作）
7. approve_if      → 审批
8. 其余            → 自动执行
```

---

## 三个值得注意的设计决策

### 1. 政策检索不用 RAG，用结构化匹配

政策库只有 5~10 条，**政策裁决需要的是确定性，不是语义相似**。
向量检索会引入不可审计的随机性。这正是 Policy Engine 存在的理由。

### 2. 「超出客服权限」是 ESCALATE，不是 DENY

退款 ¥300 对 L1 客服（权限 ¥200）：**不是违规，而是授权不足**。
DENY 会让工单死掉、客户问题无人解决；ESCALATE 把决策权交给主管。

### 3. 优惠券超权限 DENY，退款超权限 ESCALATE（刻意的不对称）

| 动作 | 超权限行为 | 理由 |
|---|---|---|
| 发券 | **DENY** | 纯赠予，无「必须发放」的义务 |
| 退款 | **ESCALATE** | 客户权利，不应因客服权限小而否决 |

**原则：涉及客户权利的动作超权限应升级；纯赠予性质的动作超权限应拒绝。**

---

## 测试覆盖

| 测试类 | 覆盖内容 |
|---|---|
| `TestPermissionModel` | Agent≤Human、未知角色 fail-safe、Agent 不可自行升级 |
| `TestReadTools` | 只读动作自动执行；不被客户情绪阻断 |
| `TestIssueCoupon` | 阈值边界、超权限、频次、单日累计、知识缺口 |
| `TestRequestRefund` | ¥200 分界线、超权限升级、异常历史、售后期限 |
| `TestRequestReplacement` | 证据要求、高价值升级 |
| `TestSendReply` | 承诺抽取 → 需审批 |
| `TestForbiddenActions` | 4 个禁止动作 × 3 种角色 |
| `TestEscalationSignals` | 对抗性用例（情绪勒索）、物流异常、丢件 |
| `TestEvaluatePlan` | 方案内重复动作 |
| `TestRevalidation` | 审批后重新校验（订单已退款 / 客户又领券） |
| `TestSafetyInvariants` | 安全红线：无政策时任何写操作都不得自动执行 |

---

## 开发状态

| 周 | 阶段 | 状态 |
|---|---|---|
| W1 | 数据与策略层（Policy Engine + 测试） | ✅ 已完成 |
| W2 | Walking Skeleton（1 条工单端到端） | ⬜ |
| W3 | Agent 层完善（4 Agent + 10 Demo Case） | ⬜ |
| W4 | 评测体系（100 条数据集 + 11 项指标） | ⬜ |
| W5 | UI（4 页面）+ 包装 | ⬜ |

---

## 边界与局限（诚实声明）

- **V0.1 明确不做**：真实支付退款、账户封禁、全自动投诉仲裁、多租户 SaaS、电话/社交媒体客服
- **数据是模拟的**：50 customers / 100 orders 为构造数据，无法验证真实流量分布
- **上生产的下一步**：用真实历史工单重跑评测集 → shadow mode 灰度 → 建立策略变更的回归测试机制
