<p align="center">
  <img src="assets/tubiao.png" width="88" alt="MC 汉化器">
</p>

<h1 align="center">MC 汉化器</h1>

<p align="center">
  把 Minecraft Java 版地图、整合包和模组(虽然目前只支持地图)<br>
  做成<strong>一份不改坏机制的中文副本</strong>
</p>


<p align="center">
  <img alt="Windows" src="https://img.shields.io/badge/系统-Windows-0078D6?style=flat-square&logo=windows&logoColor=white">
  <img alt="Minecraft Java" src="https://img.shields.io/badge/游戏-Java_版-62B47A?style=flat-square">
  <img alt="Status" src="https://img.shields.io/badge/状态-持续更新-e8b84b?style=flat-square">
</p>
<p align="center">
  <a href="#怎么用">怎么用</a>
  ·
  <a href="#使用前请知道">使用前请知道</a>
  ·
  <a href="#接下来会做什么">接下来会做什么</a>
</p>

<p align="center">
  <img src="design/shouye.png" alt="主界面：左侧布置任务，右侧查看日志和预览" width="900">
</p>


---

## 这是什么

上传文件，配好翻译api，然后坐着喝茶等翻译结束喵awa

它会：

1. **复制**你的文件，不改原件
2. **找出**告示牌、书、物品说明、语言文件等看得见的字
3. **尽量沿用**包里已有的中文和官方译名
4. **把剩下的**发给你配置的 AI 来译
5. **检查后再打包**，命令、坐标、物品 ID 这类机制内容保持原样，防止地图被破坏

适合想要玩国外地图但是苦于翻译问题的玩家喵~

## 怎么用

### 打开程序

如果已经有 `mc汉化器.exe`，双击运行即可。

还没有现成程序时，在 Windows 上用 Python 3.11 或更高版本启动：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[gui]"
python scripts/gui_entry.py
```

### 汉化一份资源

1. **选择输入**  
   拖入或点选原始的 zip / jar / mrpack，或地图、整合包文件夹。
2. **确认输出位置**  
   默认会在旁边生成新文件。请留着原始包，不要覆盖唯一底稿。
3. **填写连接**  
   打开「连接控制」，填入兼容 ChatGPT / OpenAI 格式的接口地址、密钥和模型。可以先测连接。
4. **开始**  
   不确定范围时，先点「仅扫描预览」；确认后再点「开始汉化」。
5. **检查结果**  
   看报告里哪些已经写成中文、哪些被保护没动、哪些还空着。然后进游戏抽查出生点、书、告示牌和提示文字。

<p align="center">
  <img src="design/lianjiekongzhi .png" alt="连接控制：填写接口、模型和并发" width="520">
</p>


进度走完，只表示这一轮处理结束，**不表示每一句都已译成中文**。可以在翻译结束后查看扫描预览确定翻译的内容，在问题审阅处查看未处理/无法处理的问题

## 使用前请知道

> [!WARNING]
> 要翻译的文字会发到**你自己填写的服务商**，可能产生费用。请用你信任、且余额足够的接口。

- 请用**副本**出包，保留原始地图。
- 译完请进游戏看关键流程，程序检查的是结构有没有写坏，不是玩法好不好读。
- 套了好几层的压缩包、特别老或特别新的地图，可能扫不全。
- 作者名、部分装饰字符、对机制有用的名字，常常会原样留下。
- 大图可能要较长时间，电脑也会比较吃资源。
- 中途停下再继续，会重新扫一遍并沿用已经合格的译文，不是从压缩包中间接着写。

## 接下来会做什么

| | |
| --- | --- |
| 正在做 | 把漏译、错译再收一收；服务不稳定时及时停下来，避免空打请求 |
| 接着做 | 让更大的地图更省内存；报告更好查；术语尽量对上地图版本 |
| 更远 | 处理套得更深的包；适配模组和整合包翻译 |

