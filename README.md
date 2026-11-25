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

## 接入 `.cal` 解析模块

共享源码包含查看器界面。读取真实 `.cal` 文件需要另行提供兼容的 `parse_pna_cal.py`，放在 `calset_viewer_gui.py` 同目录。查看器使用以下接口：

- `parse_cal_file(raw)` 接收 `.cal` 文件的字节，返回 `ParsedCalFile`，其中包含文件头、校准配置、元数据、标准件测量及误差项等数据。
- `ComplexTrace` 描述标准件测量等复数迹线。
- `export_results(parsed, output_dir)` 将完整结果写入指定目录。

缺少解析模块时窗口仍可启动，打开 `.cal` 时会显示安装位置提示。可读取的仪器型号、固件版本和 CalSet 类型取决于解析器支持的格式。

## 查看与导出

1. 点击 **打开 .cal**、将文件拖入窗口，或在启动命令末尾追加文件路径。
2. 在左侧树中搜索或选择文件头、元数据、标准件测量、误差项等条目。
3. 对于复数迹线，选择 **幅度（dB）/ 相位**、**线性幅度 / 相位**或**实部 / 虚部**。选中表格单元格后可用 `Ctrl+C` 复制。
4. 使用 **导出当前项** 保存迹线 CSV，或使用 **导出全部** 输出 `summary.json` 及测量迹线和误差项 CSV。
