from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .errors import VaultDrillError
from .evaluator import evaluate_plan
from .loader import apply_plan_case_inputs, load_case, load_plan, write_json_stdout
from .recommend import exhaustive_recommend, recommend


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vaultdrill",
        description="Offline immutable-snapshot recovery and sealed-data exposure drill.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    ev = sub.add_parser("evaluate", help="evaluate CASE against recovery PLAN")
    ev.add_argument("case", help="path to case JSON or a case directory")
    ev.add_argument("plan", help="path to plan JSON or a plan directory")
    ev.add_argument("--out", help="write the full JSON result to this UTF-8 path as well")
    ev.add_argument("--strict", action="store_true", help="exit 3 when the plan is not approved")

    rec = sub.add_parser("recommend", help="recommend at most 18 actions for all work sets")
    rec.add_argument("case", help="path to recommendation case JSON")
    rec.add_argument(
        "out_positional",
        nargs="?",
        help="optional path; when supplied the full JSON result is also written here",
    )
    rec.add_argument("--out", help="write the full JSON result to this UTF-8 path as well")
    rec.add_argument("--max-actions", type=int, default=18, help="action cap (default: 18)")
    rec.add_argument(
        "--exhaustive",
        action="store_true",
        help="use brute-force subset enumeration (only for small candidate sets)",
    )
    rec.add_argument("--strict", action="store_true", help="exit 3 when no feasible recommendation exists")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        case = load_case(args.case)
        if args.command == "evaluate":
            plan = load_plan(args.plan)
            case = apply_plan_case_inputs(case, plan)
            result = evaluate_plan(case, plan)
            if args.out:
                Path(args.out).write_text(
                    json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                    newline="\n",
                )
            write_json_stdout(result)
            if args.strict and not result["summary"]["approved"]:
                return 3
            return 0

        if args.command == "recommend":
            if args.max_actions < 1:
                raise VaultDrillError("INVALID_ARGUMENT", "--max-actions must be positive")
            if args.exhaustive:
                result = exhaustive_recommend(case, max_actions=args.max_actions)
            else:
                result = recommend(case, max_actions=args.max_actions)
            out_path = args.out or getattr(args, "out_positional", None)
            if out_path:
                Path(out_path).write_text(
                    json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                    newline="\n",
                )
            write_json_stdout(result)
            if args.strict and not result.get("feasible"):
                return 3
            return 0 if result.get("feasible") else 4

        parser.error(f"unknown command: {args.command}")
        return 2
    except VaultDrillError as exc:
        write_json_stdout({"ok": False, "error": exc.to_json()})
        return 2
    except OSError as exc:
        write_json_stdout(
            {"ok": False, "error": {"code": "IO_ERROR", "message": str(exc)}}
        )
        return 2
    except (ValueError, KeyError, TypeError) as exc:
        write_json_stdout(
            {
                "ok": False,
                "error": {
                    "code": "INVALID_INPUT",
                    "message": str(exc),
                    "exception_type": type(exc).__name__,
                },
            }
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
