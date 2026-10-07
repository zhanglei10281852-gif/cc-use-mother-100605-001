# 经验知识库（experience_registry）

面向装备厂智能运维团队的后端知识库：老师傅留下的故障判断与工艺诀窍以**经验条目**形式登记，
经过**指定角色复核**后进入可检索目录；相似条目自动组织**合并建议**且原始版本永久保留；
过期或被撤回的结论仍能解释**当时为何被采用**。

纯 Python 3.11 标准库实现，零外部依赖：SQLite 持久化 + `http.server` HTTP 接口 + argparse CLI。

## 核心概念

| 概念 | 说明 |
| --- | --- |
| 条目 entry | 一条经验的逻辑实体，带设备型号、工序等版本化内容，编号 `KB-0001` 起 |
| 版本 version | 条目的不可变内容快照，含设备型号、工序、适用条件、证据来源、内容、班组术语、有效期 |
| 复核任务 review_task | 每个新版本生成一条，仅 `reviewer`/`admin` 可裁决，且不能复核本人提交 |
| 合并建议 merge_suggestion | 术语归一化后相似度达阈值自动生成，接受合并后原始版本仍保留 |
| 事件 events | 全部状态迁移的审计流水，`trace`/`explain` 据此回放完整链路 |

版本状态机：

```
PENDING_REVIEW --复核通过--> APPROVED --新版本中生效--> SUPERSEDED
                               APPROVED --人工撤回-----> WITHDRAWN
                               APPROVED --有效期届满---> EXPIRED
PENDING_REVIEW --复核驳回--> REJECTED
PENDING_REVIEW --提交人撤回-> WITHDRAWN
```

只有 `APPROVED` 且被条目 current 指针引用的版本才出现在检索目录。

## 稳定性保证

- **重复导入**：`batch_key` 幂等重放（返回首次保存的结果），跨批次按 `source_ref` 唯一去重；
- **并发修订**：`base_version_no` 乐观校验 + `BEGIN IMMEDIATE` 事务，并行修订只有一个胜者，
  其余得到稳定的 `VERSION_CONFLICT`；并行复核同理得到 `REVIEW_ALREADY_DECIDED`；
- **权限变化**：角色存于数据库，每次请求实时校验，撤权后同一会话令牌立即失效于对应能力；
- **服务重启**：待审队列、合并建议、登录令牌都是数据库行，重启后原样恢复。

## 快速开始

```bash
export PYTHONPATH=src

# 初始化（幂等）并登录
python3 -m experience_registry.cli --db kb.db init-db --admin-password admin123
python3 -m experience_registry.cli --db kb.db login --username admin --password admin123
export KB_TOKEN=<返回的 token>

# 创建角色账号：工程师、复核员、只读运维
python3 -m experience_registry.cli --db kb.db user-create liwei --user-password li123456 --roles engineer,viewer
python3 -m experience_registry.cli --db kb.db user-create zhaoshi --user-password zh123456 --roles reviewer,viewer

# 工程师提交经验条目
python3 -m experience_registry.cli --db kb.db submit --token <liwei的token> \
  --equipment-model CA6140 --process-step 精车 \
  --conditions "45#钢长轴，切削液正常供给" \
  --evidence "2026-09 现场验证记录 SV-118" \
  --content "精车出现振纹时，先主轴卡死检查径向间隙，轴承发热超70度则更换"

# 复核员查看待审、复核通过后进入检索目录
python3 -m experience_registry.cli --db kb.db pending --token <zhaoshi的token>
python3 -m experience_registry.cli --db kb.db approve <task_id> --rationale "复验 50 件无振纹" --token <zhaoshi的token>

# 检索目录 / 查看完整链路 / 解释某版本
python3 -m experience_registry.cli --db kb.db search --q 振纹 --token $KB_TOKEN
python3 -m experience_registry.cli --db kb.db trace <entry_id> --token $KB_TOKEN
python3 -m experience_registry.cli --db kb.db explain <version_id> --token $KB_TOKEN

# 启动 HTTP 服务
python3 -m experience_registry.cli --db kb.db serve --port 8000
```

## HTTP 接口

除 `/api/health`、`/api/tokens` 外均需 `Authorization: Bearer <token>`。
错误响应格式稳定：`{"error": {"code", "message", "detail"}}`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/tokens` | 登录换取令牌 |
| POST | `/api/users` · `/api/users/{u}/roles` | 用户与角色管理（admin） |
| POST | `/api/entries` | 提交条目（engineer） |
| POST | `/api/entries/{id}/revisions` | 修订（需 `base_version_no`） |
| GET | `/api/entries/{id}/history` | 完整链路：版本、复核、合并、事件时间线 |
| GET | `/api/reviews/pending` | 待复核队列（reviewer） |
| POST | `/api/reviews/{id}/decision` | 复核裁决 `{"decision": "approve"\|"reject", "rationale": ...}` |
| POST | `/api/versions/{id}/withdraw` · `/expire` | 撤回 / 判定过期 |
| GET | `/api/versions/{id}/explain` | 当时为何被采用、后来为何退出 |
| GET | `/api/catalog/search?q=&equipment_model=&process_step=` | 检索目录（仅生效版本） |
| GET | `/api/merges` · POST `/api/merges/{id}/accept` · `/dismiss` | 合并建议处理 |
| POST | `/api/imports` | 批量导入 `{"batch_key", "records": [...]}`，幂等 |
| POST | `/api/maintenance/sweep-expired` | 清扫已过有效期的生效版本 |
| GET/POST | `/api/terms/aliases` | 术语别名表（如 主轴抱死→主轴卡死） |

## 批量导入格式

```json
{
  "batch_key": "erp-2026-10-07",
  "records": [
    {"source_ref": "ERP-001", "equipment_model": "CA6140", "process_step": "精车",
     "applicable_conditions": "...", "evidence_source": "...", "content": "...",
     "terms": ["一班叫法"], "valid_until": "2027-12-31"}
  ]
}
```

同一 `batch_key` 重放返回首次结果；`source_ref` 已存在的记录计入 `duplicates`，不产生重复条目。

## 测试与检查

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests run_cli.py
python3 run_cli.py   # 原有冒烟入口
```
