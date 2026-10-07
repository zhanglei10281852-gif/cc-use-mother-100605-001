"""命令行接口：工程师/复核人不经过 HTTP 也能完成全部操作并查看完整链路。

身份通过 --as <用户名>（使用内置引导令牌）或 --token <令牌> 指定。
默认输出 JSON；chain 命令加 --text 输出可读的时间线。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .db import connect, initialize
from .errors import RegistryError
from .service import RegistryService, utc_now

# 内置引导账号的便捷令牌；自建账号请用 --token
SEED_TOKENS = {
    "admin": "token-admin",
    "engineer": "token-engineer",
    "reviewer": "token-reviewer",
}


def _load_json_arg(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    if value == "-":
        return json.loads(sys.stdin.read())
    path = Path(value)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return json.loads(value)


def _load_json_list(value: str) -> list[Any]:
    path = Path(value)
    raw = path.read_text(encoding="utf-8") if path.exists() else value
    data = json.loads(raw)
    if not isinstance(data, list):
        raise SystemExit("输入必须是 JSON 数组")
    return data


def _unpack(payload: dict[str, Any]) -> Any:
    nested = payload.get("content")
    return nested if isinstance(nested, dict) else payload


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False))


def _print_chain(chain: dict[str, Any]) -> None:
    version = chain["version"]
    entry = chain["entry"]
    print(f"条目 {entry['id']}  （共 {len(entry['versions'])} 个版本）")
    print("=" * 72)
    for v in entry["versions"]:
        marker = "▶" if v["id"] == version["id"] else " "
        print(f"{marker} 版本 {v['seq']} {v['id']} [{v['status']}] "
              f"提交人={v['submitter']} 创建={v['created_at']}")
        c = v["content"]
        print(f"    标题：{c['title']}")
        print(f"    型号：{c['equipment_model']}  工序：{c['process']}")
        print(f"    条件：{'；'.join(c['conditions'])}")
        if v["reviewer"]:
            print(f"    复核/处理人：{v['reviewer']}  时间：{v['decided_at']}")
        if v["decision_note"]:
            print(f"    处理说明：{v['decision_note']}")
        if v["merged_into"]:
            print(f"    已并入：{v['merged_into']}")
        if v["superseded_by"]:
            print(f"    被替代：{v['superseded_by']}")
        print("-" * 72)
    print(f"版本 {version['id']} 的完整事件链：")
    for e in chain["timeline"]:
        detail = json.dumps(e["detail"], ensure_ascii=False) \
            if e["detail"] != {} else ""
        print(f"  #{e['seq']} {e['ts']} {e['kind']:<20} "
              f"{e['actor']}({e['actor_role']}) {detail}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="experience_registry",
                                     description="装备厂经验知识库")
    parser.add_argument("--db", default="data/registry.db", help="SQLite 路径")
    who = parser.add_mutually_exclusive_group()
    who.add_argument("--as", dest="as_user", choices=sorted(SEED_TOKENS),
                     help="以内置引导账号身份执行")
    who.add_argument("--token", help="使用账号令牌执行")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("submit", help="提交经验条目")
    p.add_argument("--file", required=True, help="内容 JSON 文件（或 - 读标准输入）")
    p.add_argument("--expires-at", default="", help="失效日期 YYYY-MM-DD")

    p = sub.add_parser("revise", help="提交修订版本")
    p.add_argument("entry_id")
    p.add_argument("--file", required=True)
    p.add_argument("--expires-at", default="")

    sub.add_parser("pending", help="列出待审版本")

    p = sub.add_parser("directory", help="查询可检索目录")
    p.add_argument("--equipment-model", default="")
    p.add_argument("--process", default="")
    p.add_argument("--keyword", default="")
    p.add_argument("--include-inactive", action="store_true")

    p = sub.add_parser("version", help="查看版本")
    p.add_argument("version_id")

    p = sub.add_parser("entry", help="查看条目全部版本")
    p.add_argument("entry_id")

    p = sub.add_parser("chain", help="查看从提交到生效的完整链路")
    p.add_argument("version_id")
    p.add_argument("--text", action="store_true", help="可读文本而非 JSON")

    p = sub.add_parser("approve", help="复核通过")
    p.add_argument("version_id")
    p.add_argument("--note", default="")

    p = sub.add_parser("reject", help="复核驳回")
    p.add_argument("version_id")
    p.add_argument("--note", required=True)

    p = sub.add_parser("withdraw", help="撤回待审版本")
    p.add_argument("version_id")
    p.add_argument("--reason", default="")

    p = sub.add_parser("expire", help="把生效版本标记过期")
    p.add_argument("version_id")
    p.add_argument("--reason", required=True)

    p = sub.add_parser("suggestions", help="查看合并建议")
    p.add_argument("--status", default="open",
                   choices=["open", "accepted", "dismissed", "all"])

    p = sub.add_parser("merge", help="按建议或直接合并版本")
    p.add_argument("--suggestion-id", type=int, default=None)
    p.add_argument("--versions", nargs="*", default=[])
    p.add_argument("--file", required=True, help="合并后的内容 JSON")
    p.add_argument("--reason", default="")

    p = sub.add_parser("dismiss", help="驳回合并建议")
    p.add_argument("suggestion_id", type=int)
    p.add_argument("--reason", default="")

    p = sub.add_parser("import", help="批量导入 JSON 数组")
    p.add_argument("--file", required=True)

    p = sub.add_parser("user-create", help="管理员创建账号")
    p.add_argument("username")
    p.add_argument("--role", required=True, choices=["admin", "engineer",
                                                      "reviewer"])
    p.add_argument("--new-token", required=True, dest="new_token")

    p = sub.add_parser("user-role", help="管理员调整角色")
    p.add_argument("username")
    p.add_argument("--role", required=True, choices=["admin", "engineer",
                                                      "reviewer"])

    p = sub.add_parser("user-set-active", help="管理员启用/停用账号")
    p.add_argument("username")
    p.add_argument("--active", choices=["true", "false"], required=True)

    sub.add_parser("users", help="管理员列出账号")

    p = sub.add_parser("synonym-add", help="管理员增加同义词组")
    p.add_argument("words", nargs="+")

    sub.add_parser("synonyms", help="查看自定义同义词组")
    sub.add_parser("audit", help="管理员查看审计日志")
    sub.add_parser("sweep-expired", help="扫描并标记到期版本")

    p = sub.add_parser("serve", help="启动 HTTP 服务")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    initialize(args.db, utc_now)

    if args.command == "serve":
        from .http import serve
        serve(args.host, args.port, args.db)
        return 0

    conn = connect(args.db)
    svc = RegistryService(conn)
    try:
        if args.command == "sweep-expired":
            _print_json({"expired": svc.sweep_expired()})
            return 0
        # 其余命令都需要身份
        token = args.token
        if token is None:
            if args.as_user is None:
                raise SystemExit("请用 --as <账号> 或 --token <令牌> 指定身份")
            token = SEED_TOKENS[args.as_user]
        actor = svc.authenticate_token(token)

        if args.command == "submit":
            payload = _load_json_arg(args.file)
            result = svc.submit(actor, _unpack(payload),
                                expires_at=args.expires_at)
        elif args.command == "revise":
            payload = _load_json_arg(args.file)
            result = svc.revise(actor, args.entry_id, _unpack(payload),
                                expires_at=args.expires_at)
        elif args.command == "pending":
            result = {"items": svc.pending()}
        elif args.command == "directory":
            result = {"items": svc.directory(
                equipment_model=args.equipment_model,
                process=args.process, keyword=args.keyword,
                include_inactive=args.include_inactive)}
        elif args.command == "version":
            result = svc.get_version(args.version_id)
        elif args.command == "entry":
            result = svc.get_entry(args.entry_id)
        elif args.command == "chain":
            result = svc.version_chain(args.version_id)
            if args.text:
                _print_chain(result)
                return 0
        elif args.command == "approve":
            result = svc.review(actor, args.version_id, True, args.note)
        elif args.command == "reject":
            result = svc.review(actor, args.version_id, False, args.note)
        elif args.command == "withdraw":
            result = svc.withdraw(actor, args.version_id, args.reason)
        elif args.command == "expire":
            result = svc.expire(actor, args.version_id, args.reason)
        elif args.command == "suggestions":
            result = {"items": svc.suggestions(args.status)}
        elif args.command == "merge":
            payload = _load_json_arg(args.file)
            result = svc.merge(actor, args.suggestion_id, args.versions,
                               _unpack(payload), args.reason)
        elif args.command == "dismiss":
            result = svc.dismiss_suggestion(actor, args.suggestion_id,
                                            args.reason)
        elif args.command == "import":
            result = {"results": svc.import_items(
                actor, _load_json_list(args.file))}
        elif args.command == "user-create":
            result = svc.create_user(actor, args.username, args.role,
                                     args.new_token)
        elif args.command == "user-role":
            result = svc.change_user_role(actor, args.username, args.role)
        elif args.command == "user-set-active":
            result = svc.set_user_active(
                actor, args.username, args.active == "true")
        elif args.command == "users":
            result = {"items": svc.list_users(actor)}
        elif args.command == "synonym-add":
            result = svc.add_synonym_group(actor, args.words)
        elif args.command == "synonyms":
            result = {"items": svc.list_synonyms()}
        elif args.command == "audit":
            result = {"items": svc.audit_log(actor)}
        else:  # pragma: no cover
            raise SystemExit(f"未知命令 {args.command}")
        _print_json(result)
        return 0
    except RegistryError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": str(exc)}},
                         ensure_ascii=False), file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
