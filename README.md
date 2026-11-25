# English Listening Video Generator

批量生成“英语听力训练”竖屏短视频，主打微信视频号及多平台发布。前期以**小学四年级“看图二选一”**题型为主，架构上预留**全学段（小学其他年级 / 初中 / 高中）与多题型**的无缝扩展能力。

## 技术路线

| 环节 | 方案 | 说明 |
| --- | --- | --- |
| 素材识别与文案生成 | 多模态大模型 API | 统一接口封装，按学段对齐教材/考纲生成听力文案与答案（先预留，后续接入） |
| 语音合成 | 火山引擎 TTS | 独立模块，支持按学段自动匹配语速 / 停顿时长 / 音色 |
| 视频合成 | Playwright 无头录制 | 本地 HTML / Canvas / Web Audio 渲染，**不依赖本地 FFmpeg**，直接录制导出视频 |

## 全学段扩展设计思路

- **学段抽象为配置**：每个学段对应 `config/curriculum/<level>.json`（如 `primary_g4` = 小学四年级）。
- **题型抽象为模板**：每种题型对应 `templates/<question_type>.html`，录制模块按题型加载对应舞台。
- **不写死学段**：`DEFAULT_LEVEL` / `DEFAULT_TYPE` 仅为默认值；CLI 参数与配置可随时切换到新增学段/题型，无需改动核心代码。
- **扩展到初中/高中**：只需在 `config/curriculum/` 新增对应 JSON（见该目录下 `README_CURRICULUM.md`），并在需要时新增题型模板即可。

## 目录职责

```
english-listening-video/
├── .env.example            # 环境变量模板（复制为 .env 后填写）
├── .gitignore              # 忽略 .env / 虚拟环境 / output 产物等
├── README.md               # 本说明
├── requirements.txt        # 基础依赖
├── main.py                 # CLI 主入口（--init-check 自检等）
├── config/
│   ├── settings.py         # 统一读取 .env、校验必填、全局默认与学段加载
│   └── curriculum/
│       ├── README_CURRICULUM.md   # 学段配置扩展说明
│       └── primary_g4.json        # 小学四年级教材/考纲参考配置
├── input/                  # 待处理题图或原始素材（含命名规范说明）
├── templates/
│   └── image_ab_choice.html # “看图二选一”竖屏舞台骨架
├── src/
│   ├── __init__.py
│   ├── question_generator.py  # 素材分析 + 分学段文案/答案生成骨架
│   ├── volc_tts.py            # 火山引擎 TTS 调用骨架
│   └── browser_recorder.py    # Playwright 无头录制骨架
└── output/                 # 生成产物（已被 git 忽略，不入库）
```

## 环境初始化

```bash
# 1. 建议使用 Python 3.10+ 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 安装 Playwright 浏览器内核
playwright install chromium

# 4. 准备环境变量
cp .env.example .env              # 然后编辑 .env 填写真实密钥

# 5. 运行环境与配置自检
python main.py --init-check
```

## Git 提交规范

- 每次阶段性任务或核心文件创建/修改完成后及时提交，避免改动堆积。
- 提交前先 `git status` 与 `git diff` 确认范围，合理暂存。
- Commit message 使用清晰中文或规范前缀，例如：
  - `chore: 初始化项目目录结构与基础配置`
  - `feat(config): 新增初中学段 curriculum 配置`
  - `fix(recorder): 修复音画同步偏移`
- **严禁提交** `.env`、敏感密钥、虚拟环境目录、缓存文件以及 `output/` 下的生成产物（已在 `.gitignore` 中忽略）。
