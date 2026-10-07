# 装备厂经验知识库（experience_registry）

把老师傅留下的**故障判断**与**工艺诀窍**沉淀为可检索、可复核、可追溯的后端知识库。
解决老厂数字化中的四个老问题：

1. **术语不统一**——同一条经验不同班组说法不同（"窜动"/"轴向位移"、"床头箱"/"主轴箱"）；
2. **依据说不清**——每次修订必须挂证据来源（现场验证批次、工单号、手册章节），并保留全部历史版本；
3. **结论有生命周期**——只有指定角色复核通过才进可检索目录；过期/撤回/驳回/被合并的结论仍可解释"当时为何采用"；
4. **结果要稳定**——重复导入、并发修订、权限变化、服务重启之后，数据与待审事项都有确定结果。

纯 Python 3.11 标准库 + SQLite（WAL），**无第三方依赖**。同时提供命令行与 HTTP/JSON 接口。

---

## 快速开始

```bash
# 运行测试
PYTHONPATH=src python3 -m unittest discover -s tests -v

# 编译检查
python3 -m compileall -q src tests run_cli.py

# 命令行（数据库自动初始化，内置三个引导账号）
PYTHONPATH=src python3 -m experience_registry --db data/registry.db --as engineer pending

# HTTP 服务
PYTHONPATH=src python3 -m experience_registry --db data/registry.db serve --host 0.0.0.0 --port 8080
```

引导账号（仅用于本地/首次部署，请随后用管理员接口轮换）：

| 用户名 | 角色 | 令牌 |
|---|---|---|
| `admin` | 管理员 | `token-admin` |
| `engineer` | 工程师 | `token-engineer` |
| `reviewer` | 复核人 | `token-reviewer` |

HTTP 认证使用 `Authorization: Bearer <token>`。

---

## 经验内容结构

```json
{
  "title": "CW6163B 主轴轴向位移的判断方法",
  "kind": "fault_judgement",            // fault_judgement(故障判断) | process_knowhow(工艺诀窍)
  "equipment_model": "CW6163B",         // 设备型号（必填）
  "process": "主轴装配",                 // 工序（必填）
  "conditions": ["空载低速", "环境温度20-30℃"],  // 适用条件（必填，至少一条）
  "content": "……操作/判断方法……",        // 经验正文（必填）
  "conclusion": "……结论/处置阈值……",     // 结论（必填）
  "evidence_sources": [                 // 证据来源（结论必须能追溯）
    {"kind": "field_verification", "reference": "验证批次2026-09-A17",
     "verified_at": "2026-09-12", "note": "三车间复测两次"}
  ],
  "tags": ["主轴", "百分表"]
}
```

证据类型 `kind`：`field_verification`（现场验证）、`work_order`（工单）、`manual`（手册）、`other`。

---

## 生命周期与状态机

版本**内容不可变**，状态只能沿允许方向迁移，每次迁移追加一条事件：

```
                提交/修订
                   │
                   ▼
              in_review（待复核，不进目录）
        ┌──────────┼──────────────┐
   复核通过      复核驳回        提交人撤回      合并
        ▼          ▼              ▼              ▼
     active ──► rejected     withdrawn       merged（保留，指向合并版本）
       │
       ├── 新版本复核通过 ──► superseded（旧版本保留，指向新版本）
       └── 到期/人工标记 ──► expired（内容、证据、过期原因全部保留）
```

- **可检索目录**默认只含 `active` 版本；`--include-inactive` / `?include_inactive=1` 可查历史。
- 每条版本都有 `content_hash`（对规范化内容的 SHA-256），**同一内容在任何时间、任何进程哈希相同**。
- 条目 ID、版本 ID 由内容哈希确定性派生。

### 完整链路

`chain <version_id>` 返回：条目下全部版本 + 该版本从提交起的全部事件（含操作人、**操作时角色快照**、时间戳、详情），即"从提交到生效（或失效）的完整链路"。

```bash
python3 -m experience_registry --db data/registry.db --as engineer chain ver_xxxx --text
```

---

## 相似发现与合并

- 管理员可维护**同义词组**（系统已内置"窜动/轴向位移/轴窜""床头箱/主轴箱""油泵/液压泵"等）。
- 每次提交后，系统把新内容与各条目当前代表版本（生效优先，否则待审）做**术语归一化 + 字符二元组 Jaccard 相似度**比较，超阈值（0.34）自动生成合并建议，并附可读理由（型号一致/工序一致/适用条件重叠）。
- 复核人可以：
  - **按建议合并**：给出合并后的内容，系统直接生成一个 `active` 的合并版本；两个源版本置 `merged` 并通过 `merged_into` 指向新版本，**原始版本与证据全部保留**；
  - **驳回建议**（填写理由）；
  - 也可直接对任意 2+ 个版本发起合并。
- 相似度计算完全确定、可解释，不依赖外部模型。

---

## 稳定性保证

| 风险 | 保证机制 |
|---|---|
| **重复导入** | `content_hash` 全局唯一 + `BEGIN IMMEDIATE` 事务；重复提交/重放整批都稳定返回既有版本，不产生新数据。导入整批同成败（一条非法全部回滚），可安全重放。 |
| **并发修订** | 每个条目同一时刻**只允许一个待审版本**；写事务由 SQLite 写锁串行化，竞争方得到明确的 `409 conflict` 或 `503 busy`，不会出现两个待审版本。版本带 `rev` 乐观锁，并发状态迁移只有一个成功。 |
| **权限变化** | 角色在每次操作时实时读取，调整/停用立即生效，无需重新登录；事件中固化 `actor_role` 快照，历史链路反映"当时谁以什么角色做的"。账号/角色变更全部进审计日志。 |
| **服务重启** | WAL + `synchronous=FULL`，已提交事务不丢；待审事项、建议、账号全部从磁盘重建；HTTP 启动钩子自动扫描到期版本。未提交事务在崩溃后回滚。 |
| **过期结论** | 支持 `expires_at`；启动时与 `sweep-expired` 接口幂等扫描；人工/自动过期均记录原因，内容与证据不删除。 |

---

## HTTP 接口

基址 `http://host:8080`，请求/响应均为 JSON。除 `/v1/health` 外都需要 Bearer 令牌。

| 方法 | 路径 | 角色 | 说明 |
|---|---|---|---|
| GET | `/v1/health` | - | 健康检查 |
| POST | `/v1/entries` | engineer/admin | 提交经验（重复内容返回既有版本） |
| POST | `/v1/entries/{id}/revisions` | engineer/admin | 提交修订版本 |
| GET | `/v1/entries/{id}` | 任意 | 条目全部版本与事件链 |
| GET | `/v1/versions/{id}` | 任意 | 查看版本 |
| GET | `/v1/versions/{id}/chain` | 任意 | 提交到生效的完整链路 |
| GET | `/v1/pending` | 任意 | 待审队列（重启后不变） |
| GET | `/v1/directory` | 任意 | 可检索目录（支持 `equipment_model`/`process`/`keyword`/`include_inactive`/`limit`） |
| POST | `/v1/versions/{id}/approve` | reviewer/admin | 复核通过（可带 `note`） |
| POST | `/v1/versions/{id}/reject` | reviewer/admin | 驳回（`note` 必填） |
| POST | `/v1/versions/{id}/withdraw` | 提交人/admin | 撤回待审版本 |
| POST | `/v1/versions/{id}/expire` | reviewer/admin | 标记生效版本过期（`reason` 必填） |
| GET | `/v1/suggestions?status=open` | 任意 | 合并建议 |
| POST | `/v1/suggestions/{id}/merge` | reviewer/admin | 按建议合并，body 为 `{"content":{...},"reason":""}` |
| POST | `/v1/suggestions/{id}/dismiss` | reviewer/admin | 驳回建议 |
| POST | `/v1/merges` | reviewer/admin | 直接合并 `{"version_ids":[...],"content":{...}}` |
| POST | `/v1/imports` | engineer/admin | 批量导入 `{"items":[...]}`，幂等可重放 |
| GET | `/v1/synonyms` | 任意 | 自定义同义词组 |
| POST | `/v1/admin/synonyms` | admin | 新增同义词组 |
| GET/POST | `/v1/admin/users` | admin | 列出/创建账号 |
| PATCH | `/v1/admin/users/{name}` | admin | 调整 `role` 或 `active` |
| GET | `/v1/admin/audit` | admin | 审计日志 |
| POST | `/v1/admin/sweep-expired` | 任意（系统动作） | 立即扫描到期版本 |

错误响应统一为 `{"error":{"code":"...","message":"..."}}`，状态码：400 校验错误、401 未认证、403 无权限、404 不存在、409 冲突、503 数据库繁忙（可重试）。

示例：

```bash
# 批量导入（文件是 JSON 数组）
curl -s localhost:8080/v1/imports -H "Authorization: Bearer token-engineer" \
  -H 'Content-Type: application/json' --data @examples/import_sample.json

# 检索目录
curl -s "localhost:8080/v1/directory?equipment_model=CW6163B&keyword=轴承" \
  -H "Authorization: Bearer token-engineer"
```

---

## 命令行一览

```bash
# 提交 / 修订 / 待审 / 目录 / 链路
xpr --as engineer submit --file exp.json
xpr --as engineer revise ent_xxxx --file exp_v2.json
xpr --as reviewer pending
xpr --as engineer directory --equipment-model CW6163B --keyword 轴承
xpr --as engineer chain ver_xxxx --text

# 复核与生命周期
xpr --as reviewer approve ver_xxxx --note "证据齐全"
xpr --as reviewer reject  ver_xxxx --note "缺少现场验证"
xpr --as engineer withdraw ver_xxxx --reason "补充证据"
xpr --as reviewer expire ver_xxxx --reason "新机型不再适用"

# 合并
xpr --as reviewer suggestions
xpr --as reviewer merge --suggestion-id 1 --file merged.json --reason "统一术语"
xpr --as reviewer dismiss 1 --reason "工序不同"

# 批量导入（幂等可重放）
xpr --as engineer import --file examples/import_sample.json

# 账号与术语（admin）
xpr --as admin user-create zhao --role reviewer --new-token token-zhao
xpr --as admin user-role zhao --role engineer
xpr --as admin user-set-active zhao --active false
xpr --as admin users
xpr --as admin synonym-add 振动大 震动大 振动异常
xpr --as admin audit

xpr serve --host 0.0.0.0 --port 8080   # 或 python3 -m experience_registry ...
```

（`xpr` 指 `PYTHONPATH=src python3 -m experience_registry --db data/registry.db`；
内容 JSON 也可用 `--file -` 从标准输入读入。）

---

## 代码结构

```
src/experience_registry/
  errors.py       # 业务错误与 HTTP 状态码
  hashing.py      # 确定性 canonical JSON / SHA-256
  models.py       # 内容结构、校验、状态机
  similarity.py   # 同义词归一化与相似度
  db.py           # SQLite schema、WAL、写事务、种子数据
  service.py      # 全部业务用例（事务边界在此）
  http.py         # 标准库 REST/JSON 接口
  cli.py          # 命令行接口
tests/            # 33 个测试：流程、规则、并发、重启、HTTP 端到端
```

## 设计取舍

- **内容不可变 + 只增事件**：历史结论永不被覆盖，天然支持"解释当时为何采用"与审计。
- **去重发生在写入路径而非查询时**：重复数据永远不会落库，导入结果与执行次数无关。
- **写串行、读并行**：WAL 下读不阻塞；所有读改写序列包在一个 `BEGIN IMMEDIATE` 中。
- **相似度刻意简单确定**：字符二元组 Jaccard 对中文短语稳健、零依赖、结果可复核；阈值与同义词组可调，避免黑盒打分。
