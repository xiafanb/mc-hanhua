# Zelda 专名词条（待人工审校）

用途：把 `The Legend of Zelda` 系列地图（如 RoH）里的专名固定到**任天堂官方简中**写法，避免同一名字在一张图里出现四种译法。

用法（可选，不自动生效）：

```powershell
$env:PYTHONPATH = "src"
python scripts/cli_entry.py translate "<map.zip>" --out "<map_zh_cn.zip>" --glossary assets/zelda-terms/zh_cn.json
# 桌面端：把该文件作为词库文件选择
```

> CLI 的 `translate` 子命令**不会**读取 `<输出包>.manual.json` 侧车文件（只有桌面 / WebUI 控制器会加载它）。
> 需要把人工修订显式传进去时，用归档 API：`translate_archive_to_file(input_zip, out_zip, manual_targets=<dict>)`；
> 零 API 复查时再加 `max_ai=0`，或者不要设置 `MC_HANHUA_TRANSLATOR`（默认 provider 只查词库、不联网）。

约定：

- **不自动映射**：这张表只在你显式作为词库传入时生效；非 Zelda 地图不会把 `Link` 译成「林克」。
- **先审校再使用**：`note` 里标「待人工确认」的条目仍需人工核对任天堂官方简中写法后使用；其余条目依据官方简中，但同样建议抽查。
- **来源与依据**：任天堂官方简体中文（《旷野之息》《王国之泪》等）。实包取证（RoH 交付包中同一英文名的中文变体）见 [专名审校表](../../docs/diagnostics/ZELDA_TERMS_REVIEW_2026-09-23.md)。
- 词条只列**专名**，不含普通名词与句子；句子里的用法差异由术语机制按优先级处理（`priority`），不覆盖上下文改写。
