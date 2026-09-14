# 内置 Minecraft 官方译名

来源：Mojang Java Edition 1.21.5 官方版本清单、客户端 JAR 内 en_us.json、资源索引指向的 zh_cn.json。
下载地址、客户端和中文资源 SHA-1 见 metadata.json。下载脚本逐项校验官方 SHA-1，客户端仅在内存中解包，不随项目保存。

7159 条英文键与官方中文配对。运行时默认离线加载；显式指定其他术语目录时仍优先使用指定版本。此基线不表示自动识别了用户地图版本。原版键值精确命中直接复用，模组自定义词不因包含原版普通词而强制替换。

复现：`python scripts/fetch_official_terms.py`。打包脚本将此目录包含在 EXE 中。
