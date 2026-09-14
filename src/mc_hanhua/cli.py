from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .glossary import Glossary
from .pipeline import run_translation, scan, translate_archive_to_file


def _print_summary(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_scan(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    if not root.exists():
        print(f"Input does not exist: {root}", file=sys.stderr)
        return 2
    if root.is_file():
        print("scan currently expects an extracted directory. Use translate for archives.", file=sys.stderr)
        return 2
    result = scan(root)
    _print_summary(
        {
            "root": str(root),
            "text_units": len(result.text_units),
            "warnings": result.warnings,
            "high_risk": sum(1 for unit in result.text_units if unit.risk_level.value == "high"),
            "files": sorted({unit.file_path for unit in result.text_units}),
        }
    )
    return 0


def cmd_translate(args: argparse.Namespace) -> int:
    input_path = Path(args.path).resolve()
    output_path = Path(args.out).resolve()
    glossary_path = Path(args.glossary).resolve() if args.glossary else None
    memory_path = Path(args.memory).resolve() if args.memory else None
    if input_path.is_file() and output_path.suffix.lower() in {".zip", ".jar", ".mrpack"}:
        report = translate_archive_to_file(
            input_path,
            output_path,
            glossary_path=glossary_path,
            memory_path=memory_path,
            max_ai=args.max_ai,
        )
    else:
        report = run_translation(
            input_path,
            output_path,
            glossary_path=glossary_path,
            memory_path=memory_path,
            max_ai=args.max_ai,
        )
    _print_summary(
        {
            "input": str(report.input_path),
            "output": str(report.output_path),
            "scanned": report.scanned,
            "translated": report.translated,
            "reused": report.reused,
            "skipped": report.skipped,
            "high_risk": report.high_risk,
            "warnings": report.warnings,
        }
    )
    return 0


def cmd_glossary_import(args: argparse.Namespace) -> int:
    glossary = Glossary.from_file(Path(args.path).resolve())
    _print_summary({"entries": len(glossary.entries)})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mc-hanhua", description="Minecraft Java localization helper")
    sub = parser.add_subparsers(dest="command", required=True)

    scan_parser = sub.add_parser("scan", help="Scan an extracted pack/map/mod directory")
    scan_parser.add_argument("path")
    scan_parser.set_defaults(func=cmd_scan)

    translate_parser = sub.add_parser("translate", help="Copy, translate, and write a localized output")
    translate_parser.add_argument("path")
    translate_parser.add_argument("--out", required=True)
    translate_parser.add_argument("--glossary")
    translate_parser.add_argument("--memory")
    translate_parser.add_argument("--max-ai", type=int)
    translate_parser.set_defaults(func=cmd_translate)

    build_parser_ = sub.add_parser("build", help="Alias of translate for this MVP")
    build_parser_.add_argument("path")
    build_parser_.add_argument("--out", required=True)
    build_parser_.add_argument("--glossary")
    build_parser_.add_argument("--memory")
    build_parser_.add_argument("--max-ai", type=int)
    build_parser_.set_defaults(func=cmd_translate)

    glossary_parser = sub.add_parser("glossary", help="Glossary utilities")
    glossary_sub = glossary_parser.add_subparsers(dest="glossary_command", required=True)
    import_parser = glossary_sub.add_parser("import", help="Validate a glossary file")
    import_parser.add_argument("path")
    import_parser.set_defaults(func=cmd_glossary_import)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
