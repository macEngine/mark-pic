# md2xhs — Markdown → 小红书图片（纯命令行）

把一个 Markdown 文件直接渲染成适合小红书的卡片图（默认 3:4，自动分页成多张）。
**不打开浏览器**，纯 Python + Pillow 排版，给路径就出图。

## 依赖

- Python 3.9+
- Pillow：`pip3 install Pillow`
- 字体（macOS 自带）：Hiragino Sans GB、Songti、Menlo、Apple Color Emoji，
  以及 Arial Unicode（`/Library/Fonts/Arial Unicode.ttf`，用于 IPA 音标回退）

## 用法

```bash
# 最简：给 md 路径，默认配置出图到 ./output/
python3 md2xhs.py "/path/to/某篇.md"

# 指定输出目录 / 主题 / 格式
python3 md2xhs.py article.md -o ./out --theme sunset --format jpg
```

生成文件名形如 `<md文件名>_01.png`、`_02.png` …，并打印每张的绝对路径。

## 参数

| 参数 | 默认 | 说明 |
|------|------|------|
| `md` | （必填） | Markdown 文件路径 |
| `-o, --out` | `output` | 输出目录 |
| `--theme` | `indigo` | 配色：`indigo` `sunset` `ocean` `forest` `ink` `peach`（影响标题条/表头/引用条等强调色） |
| `--width` | `1080` | 图片宽度（px） |
| `--margin` | `0` | 外边框宽度px。`0`=白底满版铺满（默认，无边框）；`>0` 时显示渐变边框+圆角白卡 |
| `--ratio-w` / `--ratio-h` | `3` / `4` | 宽高比，默认 3:4。改 `--ratio-w 1 --ratio-h 1` 即 1:1 |
| `--scale` | `2` | 超采样倍数，越大越清晰（也越慢） |
| `--format` | `png` | `png` 或 `jpg` |

## 分页规则

- **流式排版**：内容连续填充，多个 `## 段落` 会自然连排在同一张图（例如词汇卡片 + 今日例句同在第 1 页），填满后才换页。
- 需要强制分页时，在 md 中插入 `---` 或 `<!-- more -->`。
- 表格、图片、引用块整块不跨页；标题不会被孤立在卡片底部。

## 支持的 Markdown

标题（# / ## / ###）、段落、**加粗**、`行内代码/标签`、引用块（衬线体）、
有序/无序列表、表格（表头高亮+自动列宽+单元格换行）、图片（本地或 http 链接，
自动下载、圆角、限高）、emoji（彩色）、IPA 音标。

## 备注

- 远程图片下载失败会自动跳过并继续（stderr 给 warning）。
- 想换字体改文件顶部 `F_CJK / F_SERIF / F_MONO / F_IPA` 常量即可。
