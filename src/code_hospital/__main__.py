"""CLI 入口：`hosp serve`（MCP stdio）与 `hosp check`（终端冒烟）。

hosp check：不经 MCP，直接对当前仓库完成「挂号 + 检验」，打印报告路径——
用于 P0 验收「送医→检验→报告直接贴进 PR」。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys


def main() -> None:
    parser = argparse.ArgumentParser(prog="hosp", description="Code Hospital CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("serve", help="启动 MCP server（stdio，一般由宿主拉起）")

    check = sub.add_parser("check", help="冒烟：挂号 + 检验，输出检验报告路径")
    check.add_argument("--repo", default="", help="仓库根目录（默认 cwd）")
    check.add_argument("--scope-type", default="staged",
                       choices=["repo", "paths", "staged", "commit", "range"])
    check.add_argument("--paths", nargs="*", default=[], help="scope_type=paths 时的路径清单")
    check.add_argument("--commit", default="", help="scope_type=commit 时的提交（默认 HEAD）")
    check.add_argument("--range", dest="range_", default="", help="scope_type=range，如 main..HEAD")
    check.add_argument("--title", default="CLI 冒烟检查")
    check.add_argument("--tools", nargs="*", default=None, help="指定设备（默认 auto）")
    check.add_argument("--json", action="store_true", help="以 JSON 打印完整出参")

    args = parser.parse_args()

    if args.command == "serve":
        from .api.mcp_server import serve

        serve()
        return

    if args.command == "check":
        asyncio.run(_check(args))


async def _check(args) -> None:
    from .core.errors import HospitalError
    from .departments import reception, scanner

    scope = {"paths": args.paths, "commit": args.commit, "range": args.range_}
    try:
        reg = await reception.register(
            title=args.title,
            scope_type=args.scope_type,
            scope=scope,
            force=False,
            project_root=args.repo,
        )
    except HospitalError as e:
        print(json.dumps(e.payload(), ensure_ascii=False, indent=2))
        sys.exit(2)

    try:
        res = await scanner.scan(
            case_id=reg["case_id"], tools=args.tools, project_root=args.repo
        )
    except HospitalError as e:
        print(json.dumps(e.payload(), ensure_ascii=False, indent=2))
        sys.exit(2)

    if args.json:
        print(json.dumps({"register": reg, "scan": res}, ensure_ascii=False, indent=2))
        return

    findings = res["findings"]
    print(f"挂号成功: {reg['case_id']}（分诊 {reg['triage_level']}，"
          f"查重 {len(reg['duplicate_cases'])} 条历史病例）")
    print(f"检验完成: {findings['total']} 条发现")
    for f in findings["data"][:10]:
        print(f"  [{f['severity']}] {f['rule_id']} {f['file_path']}:{f['line_start']} {f['message'][:60]}")
    if findings["total"] > 10:
        print(f"  … 其余 {findings['total'] - 10} 条见报告")
    print(f"检验报告（可直接贴进 PR）: {res['report_path']}")
    if res["suggested_acceptance_commands"]:
        print("探测到验收命令建议（未执行）:")
        for s in res["suggested_acceptance_commands"]:
            print(f"  {s['alias']}: {s['command']}  （依据 {s['source']}）")


if __name__ == "__main__":
    main()
