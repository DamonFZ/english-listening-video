# input/ 目录说明

本目录用于存放待处理的题图或原始素材。生成的视频产物会输出到 `output/`（已被 git 忽略），请勿在此目录放置大体积二进制素材的长期版本，建议素材另行管理或按需放入。

## 命名规范

为便于流水线自动识别学段、题型与批次，建议素材文件名遵循：

```
<level>__<question_type>__<unit_or_topic>__<seq>.<ext>
```

示例：

```
primary_g4__image_ab_choice__colors__01.png
primary_g4__image_ab_choice__numbers__02.jpg
```

字段说明：

- `level`：学段标识，须与 `config/curriculum/<level>.json` 一致，如 `primary_g4`。
- `question_type`：题型标识，须与 `templates/<question_type>.html` 对应，如 `image_ab_choice`。
- `unit_or_topic`：单元或主题，便于分教材对齐，如 `colors`、`numbers`、`shopping`。
- `seq`：同主题下的序号，两位数字起步，如 `01`、`02`。
- `ext`：常见图片格式 `png` / `jpg` / `jpeg` / `webp`。

## 目录组织（可选）

如需按批次隔离，可在本目录下建子文件夹，例如：

```
input/
├── primary_g4/
│   ├── colors/
│   └── numbers/
└── primary_g5/
```

解析时以文件内容与命名为主，子目录仅作人工整理用途。
