from __future__ import annotations

import json
import os
import platform
import traceback
import zipfile
from datetime import datetime
from pathlib import Path

from .archives import ARCHIVE_SUFFIXES as _ARCHIVE_SUFFIXES

_NBT_SUFFIXES = {".dat", ".mca", ".mcr", ".nbt"}


def _lang_locale(rel_path: str) -> str | None:
    """Return the locale of an assets/<modid>/lang/<locale>.<ext> path."""

    parts = rel_path.replace("\\", "/").split("/")
    for index in range(len(parts) - 3):
        if parts[index] == "assets" and parts[index + 2] == "lang" and index + 3 == len(parts) - 1:
            stem, dot, _ext = parts[index + 3].rpartition(".")
            if dot and stem:
                return stem.lower()
    return None


def _detect_export_manifest(root: Path) -> str | None:
    manifest = root / "manifest.json"
    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8-sig", errors="replace"))
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict) and data.get("manifestType") == "minecraftModpack":
            label = f"{data.get('name') or ''} {data.get('version') or ''}".strip() or manifest.name
            files = data.get("files")
            count = len(files) if isinstance(files, list) else 0
            return (
                f"检测到 CurseForge 整合包导出清单（{label}，引用 {count} 个 CurseForge 文件）："
                "模组和地图本体并不在压缩包内，启动器会按清单另行下载，因此包内没有可翻译的英文源文本。"
            )
    index = root / "modrinth.index.json"
    if index.is_file():
        try:
            data = json.loads(index.read_text(encoding="utf-8-sig", errors="replace"))
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict) and "formatVersion" in data and isinstance(data.get("files"), list):
            label = f"{data.get('name') or ''} {data.get('versionId') or ''}".strip() or index.name
            return (
                f"检测到 Modrinth 整合包导出清单（{label}，引用 {len(data['files'])} 个 Modrinth 文件）："
                "模组和地图本体并不在压缩包内，启动器会按清单另行下载，因此包内没有可翻译的英文源文本。"
            )
    return None


def _count_candidates(root: Path) -> tuple[dict[str, int], int, int]:
    """Count lang files per locale (loose and inside nested archives), NBT files and mcfunction scripts."""

    locales: dict[str, int] = {}
    nbt_candidates = 0
    script_candidates = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        locale = _lang_locale(path.relative_to(root).as_posix())
        if locale:
            locales[locale] = locales.get(locale, 0) + 1
            continue
        if suffix in _NBT_SUFFIXES:
            nbt_candidates += 1
        elif suffix == ".mcfunction":
            script_candidates += 1
        elif suffix in _ARCHIVE_SUFFIXES and zipfile.is_zipfile(path):
            try:
                with zipfile.ZipFile(path) as zf:
                    for name in zf.namelist():
                        nested_locale = _lang_locale(name)
                        if nested_locale:
                            locales[nested_locale] = locales.get(nested_locale, 0) + 1
            except (OSError, zipfile.BadZipFile):
                continue
    return locales, nbt_candidates, script_candidates


def collect_input_diagnostics(root: Path) -> list[str]:
    """Explain why a finished run may have found zero translatable texts."""

    root = Path(root)
    messages: list[str] = []
    if not root.is_dir():
        return messages

    export_warning = _detect_export_manifest(root)
    if export_warning:
        messages.append(export_warning)

    locales, nbt_candidates, script_candidates = _count_candidates(root)
    if locales.get("zh_cn"):
        messages.append(
            f"输入中已包含 {locales['zh_cn']} 个简体中文（zh_cn）语言文件：该内容可能已自带中文翻译，无需重复汉化。"
        )
    if locales.get("en_us"):
        messages.append(
            f"找到 {locales['en_us']} 个 en_us 英文语言文件，但未提取到文本：内容可能已是中文，或全部是 ID、路径等技术字段。"
        )
    elif locales:
        messages.append(
            "未找到任何 en_us 英文语言文件：提取器以 en_us 为翻译源，仅包含译文的文件不会产生待翻译文本。"
        )
    if nbt_candidates:
        messages.append(
            f"找到 {nbt_candidates} 个存档/NBT 数据文件，但未提取到文本：其中可能没有英文文本组件，或内容已是中文。"
        )
    if script_candidates:
        messages.append(f"找到 {script_candidates} 个 .mcfunction 脚本，但未提取到可见文本。")

    if export_warning:
        messages.append(
            "建议：用启动器安装该整合包后，将安装好的整个实例目录（包含 mods/ 与 saves/）作为输入重新运行。"
        )
    elif not messages:
        messages.append(
            "未找到支持的文本来源。可提取的内容包括：assets/*/lang/en_us.json 或 en_us.lang、"
            "数据包/任务/进度等 JSON 文本、.mcfunction 命令文本、存档 NBT（区块/实体/告示牌/书页）。"
            "请确认输入包含上述内容；整合包导出清单需先用启动器安装，再喂入完整实例目录。"
        )
    return messages


def log_directory() -> Path:
    base = Path(os.getenv("APPDATA") or Path.home() / "AppData" / "Roaming")
    path = base / "mc-hanhua" / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_failure_log(
    error: BaseException,
    *,
    operation: str,
    input_path: Path | None = None,
    output_path: Path | None = None,
) -> Path | None:
    """Persist actionable failures without recording credentials or request bodies."""

    try:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        path = log_directory() / f"{timestamp}-{operation}.log"
        lines = [
            f"operation: {operation}",
            f"time: {datetime.now().isoformat(timespec='seconds')}",
            f"python: {platform.python_version()}",
            f"platform: {platform.platform()}",
            f"cwd: {Path.cwd()}",
            f"input: {input_path or ''}",
            f"output: {output_path or ''}",
            "",
            "traceback:",
            "".join(traceback.format_exception(type(error), error, error.__traceback__)),
        ]
        if output_path:
            parent = output_path.parent
            lines.extend(
                [
                    "",
                    "output directory:",
                    f"path: {parent}",
                    f"exists: {parent.exists()}",
                    f"is_dir: {parent.is_dir()}",
                ]
            )
        path.write_text("\n".join(lines), encoding="utf-8")
        return path
    except OSError:
        return None


def attach_diagnostic_path(error: BaseException, log_path: Path | None) -> None:
    if log_path is not None:
        setattr(error, "diagnostic_log_path", str(log_path))
