# curriculum/ 学段配置扩展说明

本目录以**一个学段一个 JSON 文件**的方式组织教材与考纲参考配置，是产品“全学段扩展”的核心抽象层。

## 文件命名

```
config/curriculum/<level>.json
```

- `level` 为学段标识，在 `.env` 的 `DEFAULT_LEVEL` 与 CLI 的 `--level` 参数中引用。
- 已存在示例：`primary_g4.json`（小学四年级）。

## 扩展到小学其他年级 / 初中 / 高中

新增学段只需两步，**无需修改核心代码**：

1. 在本目录复制 `primary_g4.json`，重命名为目标标识，例如：
   - 小学五年级：`primary_g5.json`
   - 小学六年级：`primary_g6.json`
   - 初一：`junior_g7.json`
   - 高一：`senior_g10.json`
2. 修改其中的 `name / stage / grade / units / core_vocabulary / core_sentences / contrast_dimensions` 等字段，对齐对应学段的教材与考纲；调整 `tts.speed_ratio` 与 `tts.pause_ms` 以匹配该学段的理解节奏（高年级可加快语速、缩短停顿）。

切换默认学段：在 `.env` 中修改 `DEFAULT_LEVEL=primary_g5`，或在运行时传入 `--level primary_g5`。

## 字段约定（建议结构）

```jsonc
{
  "level": "primary_g4",      // 学段标识，与文件名一致
  "name": "小学四年级",
  "stage": "primary",         // primary | junior | senior，便于按学段批量处理
  "grade": 4,
  "tts": {
    "voice_type": "en_female_female_tutor_ms-jenny_uranus_bigtts",  // 可覆盖 .env 中的默认音色（必须是 seed-tts-2.0 兼容音色）
    "speed_ratio": 0.9,               // 语速倍率（1.0 为标准，V3 换算为 speech_rate）
    "pause_ms": 600,                  // 句间停顿（毫秒，写入 additions.silence_duration）
    "read_times": 2                   // 该学段默认朗读遍数
  },
  "units": [ { "id": "...", "title": "...", "topic": "...", "vocabulary": [...], "sentences": [...] } ],
  "core_vocabulary": [...],
  "core_sentences": [...],
  "contrast_dimensions": [ { "dimension": "...", "label": "...", "example_question": "..." } ]
}
```

## 校验与加载

- 加载逻辑见 `config/settings.py` 的 `Settings.load_curriculum(level)`。
- `--init-check` 会校验 `DEFAULT_LEVEL` 对应文件是否存在，并在缺失时给出明确报错。
- 字段采用“兜底 setdefault”策略：缺失可选子结构不会崩溃，但建议保持结构完整以获得最佳生成效果。
