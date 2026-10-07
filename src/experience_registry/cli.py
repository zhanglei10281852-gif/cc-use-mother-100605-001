"""命令行入口：运维人员通过 CLI 完成全部操作，与 HTTP 接口共用同一服务层。

用法示例：
    python3 -m experience_registry.cli --db kb.db init-db
    python3 -m experience_registry.cli --db kb.db login --username admin --password admin123
    KB_TOKEN=... python3 -m experience_registry.cli --db kb.db submit --equipment-model CA6140 ...
    KB_TOKEN=... python3 -m experience_registry.cli --db kb.db trace <entry_id>
"""

import argparse
import json
import os
import sys

from .errors import KnowledgeError
from .service import KnowledgeService

_ACTION_LABELS = {
    "USER_CREATED": "创建用户",
    "ROLE_GRANTED": "授予角色",
    "ROLE_REVOKED": "撤销角色",
    "LOGIN": "登录",
    "ENTRY_SUBMITTED": "提交条目",
    "VERSION_SUBMITTED": "提交修订版本",
    "REVIEW_APPROVED": "复核通过（进入检索目录）",
    "REVIEW_REJECTED": "复核驳回",
    "REVIEW_TASK_CANCELLED": "撤销复核任务",
    "VERSION_SUPERSEDED": "被新版本取代",
    "VERSION_WITHDRAWN": "撤回版本",
    "VERSION_EXPIRED": "版本过期",
    "MERGE_SUGGESTED": "生成合并建议",
    "MERGE_ACCEPTED": "接受合并",
    "MERGE_DISMISSED": "驳回合并建议",
    "IMPORT_COMPLETED": "批量导入完成",
    "TERM_ALIAS_SET": "设置术语别名",
}


def _print_json(payload):
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _resolve_token(service, args):
    if getattr(args, "token", None):
        return args.token
    if args.username and args.password:
        return service.authenticate(args.username, args.password)["token"]
    raise SystemExit("需要 --token（或环境变量 KB_TOKEN），或提供 --username/--password 登录")


def _add_entry_fields(parser):
    parser.add_argument("--equipment-model", required=True, help="设备型号，如 CA6140")
    parser.add_argument("--process-step", required=True, help="工序，如 精车")
    parser.add_argument("--conditions", required=True, help="适用条件")
    parser.add_argument("--evidence", required=True, help="证据来源，如 2026-09 现场验证记录")
    parser.add_argument("--content", required=True, help="经验内容")
    parser.add_argument("--terms", default="", help="班组术语，逗号分隔")
    parser.add_argument("--valid-until", default=None, help="有效期 YYYY-MM-DD")


def _entry_kwargs(args):
    return {
        "equipment_model": args.equipment_model,
        "process_step": args.process_step,
        "applicable_conditions": args.conditions,
        "evidence_source": args.evidence,
        "content": args.content,
        "terms": [t for t in args.terms.split(",") if t.strip()],
        "valid_until": args.valid_until,
    }


def _print_trace(history):
    entry = history["entry"]
    print(f"条目 {entry['label']}（{entry['id']}） 状态={entry['status']}")
    if entry["canonical_entry_id"]:
        print(f"  已并入主条目：{entry['canonical_entry_id']}（原始版本保留）")
    print("版本链：")
    for version in history["versions"]:
        line = f"  v{version['version_no']} [{version['status']}] 提交人={version['created_by']}"
        if version["reviewed_by"]:
            line += f" 复核人={version['reviewed_by']}"
        print(line)
    print("事件时间线：")
    for event in history["timeline"]:
        label = _ACTION_LABELS.get(event["action"], event["action"])
        detail = json.dumps(event["detail"], ensure_ascii=False) if event["detail"] else ""
        print(f"  [{event['ts']}] {event['actor']} {label} {detail}")


def build_parser():
    parser = argparse.ArgumentParser(prog="kb", description="装备厂经验知识库命令行")
    parser.add_argument("--db", default=os.environ.get("KB_DB", "knowledge.db"), help="数据库文件路径")
    parser.add_argument("--token", default=os.environ.get("KB_TOKEN"), help="访问令牌")
    parser.add_argument("--username", default=os.environ.get("KB_USER"))
    parser.add_argument("--password", default=os.environ.get("KB_PASSWORD"))
    # 认证参数也允许写在子命令之后（SUPPRESS 避免覆盖主解析器的值）
    auth_parent = argparse.ArgumentParser(add_help=False)
    auth_parent.add_argument("--token", default=argparse.SUPPRESS)
    auth_parent.add_argument("--username", default=argparse.SUPPRESS)
    auth_parent.add_argument("--password", default=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add(name, **kwargs):
        return sub.add_parser(name, parents=[auth_parent], **kwargs)

    p = add("init-db", help="初始化数据库并创建首位管理员（幂等）")
    p.add_argument("--admin-name", default="admin")
    p.add_argument("--admin-password", default="admin123")

    p = add("login", help="登录并输出访问令牌")

    p = add("serve", help="启动 HTTP 服务")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)

    p = add("user-create", help="创建用户")
    p.add_argument("name", help="新用户名")
    p.add_argument("--user-password", required=True)
    p.add_argument("--display-name", default=None)
    p.add_argument("--roles", default="viewer", help="逗号分隔：admin,engineer,reviewer,viewer")

    p = add("role-grant", help="授予角色")
    p.add_argument("name")
    p.add_argument("role")

    p = add("role-revoke", help="撤销角色")
    p.add_argument("name")
    p.add_argument("role")

    add("users", help="用户列表")

    p = add("submit", help="提交经验条目")
    _add_entry_fields(p)
    p.add_argument("--source-ref", default=None, help="外部来源编号（用于去重）")

    p = add("revise", help="基于最新版本修订条目")
    p.add_argument("entry_id")
    p.add_argument("--base-version", type=int, required=True, help="基于的版本号，须等于当前最新")
    _add_entry_fields(p)

    add("pending", help="待复核队列")

    p = add("approve", help="复核通过")
    p.add_argument("task_id")
    p.add_argument("--rationale", required=True, help="复核依据")

    p = add("reject", help="复核驳回")
    p.add_argument("task_id")
    p.add_argument("--rationale", required=True)

    p = add("withdraw", help="撤回版本")
    p.add_argument("version_id")
    p.add_argument("--reason", required=True)

    p = add("expire", help="人工判定版本过期")
    p.add_argument("version_id")
    p.add_argument("--reason", default=None)

    add("sweep", help="清扫已过有效期的生效版本")

    p = add("search", help="检索目录（仅复核通过且生效的版本）")
    p.add_argument("--q", default=None)
    p.add_argument("--equipment-model", default=None)
    p.add_argument("--process-step", default=None)

    p = add("show", help="查看条目当前状态")
    p.add_argument("entry_id")

    p = add("trace", help="查看条目从提交到生效的完整链路")
    p.add_argument("entry_id")

    p = add("explain", help="解释某版本当时为何被采用、后来为何退出")
    p.add_argument("version_id")

    p = add("merges", help="合并建议列表")
    p.add_argument("--status", default=None)

    p = add("merge-accept", help="接受合并建议")
    p.add_argument("suggestion_id")
    p.add_argument("--canonical", required=True, help="保留为主条目的 entry_id")
    p.add_argument("--note", default=None)

    p = add("merge-dismiss", help="驳回合并建议")
    p.add_argument("suggestion_id")
    p.add_argument("--reason", required=True)

    p = add("import", help="批量导入 JSON 文件（幂等）")
    p.add_argument("file", help='内容为 {"batch_key": ..., "records": [...]}')

    p = add("alias-add", help="添加术语别名")
    p.add_argument("alias")
    p.add_argument("canonical")

    add("aliases", help="术语别名表")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    service = KnowledgeService(args.db)
    try:
        if args.cmd == "init-db":
            _print_json(service.bootstrap_admin(args.admin_name, args.admin_password))
            return 0
        if args.cmd == "login":
            _print_json(service.authenticate(args.username, args.password))
            return 0
        if args.cmd == "serve":
            from .httpapi import make_server

            server = make_server(service, args.host, args.port)
            print(f"HTTP 服务已启动：http://{args.host}:{args.port}（数据库 {args.db}）")
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                server.shutdown()
            return 0

        token = _resolve_token(service, args)
        if args.cmd == "user-create":
            _print_json(service.create_user(
                token, username=args.name, password=args.user_password,
                display_name=args.display_name,
                roles=[r for r in args.roles.split(",") if r]))
        elif args.cmd == "role-grant":
            _print_json(service.change_role(token, args.name, args.role, "grant"))
        elif args.cmd == "role-revoke":
            _print_json(service.change_role(token, args.name, args.role, "revoke"))
        elif args.cmd == "users":
            _print_json(service.list_users(token))
        elif args.cmd == "submit":
            _print_json(service.submit_entry(token, source_ref=args.source_ref, **_entry_kwargs(args)))
        elif args.cmd == "revise":
            _print_json(service.revise_entry(token, args.entry_id,
                                             base_version_no=args.base_version, **_entry_kwargs(args)))
        elif args.cmd == "pending":
            _print_json(service.pending_reviews(token))
        elif args.cmd == "approve":
            _print_json(service.decide_review(token, args.task_id, decision="approve",
                                              rationale=args.rationale))
        elif args.cmd == "reject":
            _print_json(service.decide_review(token, args.task_id, decision="reject",
                                              rationale=args.rationale))
        elif args.cmd == "withdraw":
            _print_json(service.withdraw_version(token, args.version_id, reason=args.reason))
        elif args.cmd == "expire":
            _print_json(service.expire_version(token, args.version_id, reason=args.reason))
        elif args.cmd == "sweep":
            _print_json(service.sweep_expired(token))
        elif args.cmd == "search":
            _print_json(service.search_catalog(token, q=args.q,
                                               equipment_model=args.equipment_model,
                                               process_step=args.process_step))
        elif args.cmd == "show":
            _print_json(service.get_entry(token, args.entry_id))
        elif args.cmd == "trace":
            _print_trace(service.entry_history(token, args.entry_id))
        elif args.cmd == "explain":
            _print_json(service.explain_version(token, args.version_id))
        elif args.cmd == "merges":
            _print_json(service.list_merge_suggestions(token, status=args.status))
        elif args.cmd == "merge-accept":
            _print_json(service.accept_merge(token, args.suggestion_id,
                                             canonical_entry_id=args.canonical, note=args.note))
        elif args.cmd == "merge-dismiss":
            _print_json(service.dismiss_merge(token, args.suggestion_id, reason=args.reason))
        elif args.cmd == "import":
            with open(args.file, encoding="utf-8") as fh:
                payload = json.load(fh)
            _print_json(service.import_entries(token, batch_key=payload.get("batch_key"),
                                               records=payload.get("records") or []))
        elif args.cmd == "alias-add":
            _print_json(service.add_term_alias(token, alias=args.alias, canonical=args.canonical))
        elif args.cmd == "aliases":
            _print_json(service.list_term_aliases(token))
        return 0
    except KnowledgeError as exc:
        print(json.dumps({"error": exc.to_dict()}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
