#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dcmget.licensing import (  # noqa: E402
    LicenseError,
    issue_license,
    normalize_machine_code,
    validate_license,
)


DEFAULT_PRIVATE_KEY = Path.home() / ".dcmget-license" / "ed25519-private.pem"


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("日期必须采用 YYYY-MM-DD 格式") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="为指定机器生成 DcmGet 离线注册码（私钥不会写入注册码）"
    )
    parser.add_argument("machine_code", nargs="?", help="客户软件注册页显示的机器码")
    parser.add_argument("--customer", help="客户或机构名称")
    parser.add_argument("--expires", type=parse_date, help="到期日 YYYY-MM-DD；省略为永久")
    parser.add_argument("--private-key", type=Path, default=DEFAULT_PRIVATE_KEY)
    parser.add_argument("--output", type=Path, help="同时把注册码写入文件")
    parser.add_argument("--raw", action="store_true", help="仅输出注册码，便于脚本调用")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    machine = args.machine_code or input("客户机器码：").strip()
    customer = args.customer or input("客户名称：").strip()
    try:
        machine = normalize_machine_code(machine)
        if not args.private_key.expanduser().is_file():
            raise LicenseError(f"未找到授权私钥：{args.private_key.expanduser()}")
        token = issue_license(
            args.private_key,
            machine,
            customer,
            expires_on=args.expires,
        )
        validate_license(token, expected_machine_code=machine)
    except (OSError, LicenseError, ValueError) as exc:
        print(f"生成失败：{exc}", file=sys.stderr)
        return 1

    if args.output:
        output = args.output.expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(token + "\n", encoding="utf-8", newline="\n")
    if args.raw:
        print(token)
    else:
        expiry = args.expires.isoformat() if args.expires else "永久"
        print(f"客户：{customer}")
        print(f"机器码：{machine}")
        print(f"有效期：{expiry}")
        print("注册码：")
        print(token)
        if args.output:
            print(f"已写入：{args.output.expanduser()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
