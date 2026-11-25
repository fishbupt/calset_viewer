# CalSet Viewer

用于查看 Keysight PNA / PNA-X `.cal` 校准文件的 PyQt6 桌面工具。界面可按类别浏览校准数据，并把复数迹线显示为曲线和数据表格。

## 安装与启动

需要 Python 3.10 或更高版本，以及 [uv](https://docs.astral.sh/uv/getting-started/installation/)。在有图形桌面环境的机器上执行：

```sh
git clone https://github.com/fishbupt/calset_viewer.git
cd calset_viewer
uv sync --locked
uv run --locked python calset_viewer_gui.py
```

项目通过 `pyproject.toml` 声明 PyQt6 和 Matplotlib 依赖，并通过 `uv.lock` 锁定依赖版本。

## 命令行解析与导出

`parse_pna_cal.py` 已随项目提供，仅使用 Python 标准库。在仓库目录可直接执行：

```sh
python parse_pna_cal.py calset_101pts.cal
python parse_pna_cal.py calset_101pts.cal --export-dir ./cal_out
```

第一条命令打印校准文件摘要。第二条命令还会写出 `summary.json`，并按文件中解析到的数据生成 `raw_measurements.csv`、`mapped_measurements.csv` 和 `error_terms.csv`。

解析器按已识别的 MFC CArchive 布局读取文件头、激励与校准设置、元数据、标准件测量和误差项。仪器型号、固件版本和 CalSet 类型的兼容性取决于文件布局。

格式布局、样本字段和解析覆盖范围见 [PNA / PNA-X `.cal` 二进制数据结构](docs/pna-cal-file-format.md)。

## 查看与导出

1. 点击 **打开 .cal**、将文件拖入窗口，或在启动命令末尾追加文件路径。
2. 在左侧树中搜索或选择文件头、元数据、标准件测量、误差项等条目。
3. 对于复数迹线，选择 **幅度（dB）/ 相位**、**线性幅度 / 相位**或**实部 / 虚部**。选中表格单元格后可用 `Ctrl+C` 复制。
4. 使用 **导出当前项** 保存迹线 CSV，或使用 **导出全部** 输出 `summary.json` 及测量迹线和误差项 CSV。
