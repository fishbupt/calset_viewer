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

CalSet 保存的内容、各项数据的用途及自研 VNA 的保存要求见 [Keysight VNA CalSet 内容及用途说明](docs/pna-calset-content-and-purpose.md)。

## 查看与导出

1. 点击 **打开 .cal**、将文件拖入窗口，或在启动命令末尾追加文件路径。
2. 在左侧树中搜索或选择文件头、元数据、标准件测量、误差项等条目。
3. 对于复数迹线，选择 **幅度（dB）/ 相位**、**线性幅度 / 相位**或**实部 / 虚部**。选中表格单元格后可用 `Ctrl+C` 复制。
4. 使用 **导出当前项** 保存迹线 CSV，或使用 **导出全部** 输出 `summary.json` 及测量迹线和误差项 CSV。

## Differential IQ 校准数据

包含多条 CalSet 记录的 DIQ 文件可在 **映射标准件测量** 下分别查看。界面和 CSV 保留迹线原有点序和频率值。频率值重复或未按升序排列时，曲线以点索引为横轴，数据表仍显示逐点频率。全量导出的 `mapped_measurements.csv` 包含 `calset_name` 与 `freq_hz` 两列，便于区分同名迹线。

**误差项** 按 CalSet 显示 `CrossTalk`、`Directivity`、`TransmissionTracking` 等具名复数迹线。全量导出写入 `diq_error_terms.csv`，包含 CalSet 名称、误差项名称、点索引、频率（Hz）、实部和虚部。记录缺少可识别的频率轴时，图形以点索引为横轴，CSV 的 `freq_hz` 留空。

可通过 HiSLIP 从 PNA-X 读取一条误差项。在运行 PNA-X 软件的电脑上，从仓库根目录执行下方命令，按编号选择 CalSet 和误差项。默认地址是 `TCPIP0::127.0.0.1::hislip0::INSTR`，默认输出 `term.txt`。电脑需要可用的 VISA 后端，例如 Keysight VISA。

```sh
uv run --locked --with pyvisa python tools/export_error_term.py
```

也可用 `--calset` 和 `--term` 按名称导出，用 `--list-calsets` 或 `--list-terms` 查看目录。用 `--resource` 指定其它仪器地址，用 `--output` 指定输出文件。每行保存一个频点的实部、虚部，保留点序。选择同一 CalSet 内随频点变化的一条误差项，再运行：

```sh
uv run --locked python tools/locate_error_term.py calset_diq_ch01.cal term.txt
```

工具报告符合整条参考曲线的文件偏移和数值布局，供验证误差项区段。

## 导入日志

导入失败时，窗口底部会自动展开 **导入日志**。点击 **复制日志**，即可粘贴本次运行的诊断记录。也可通过 **查看 → 导入日志** 手动打开面板。

日志记录文件名、读取字节数、解析区段与偏移、异常堆栈和导入结果。点击 **打开日志目录** 可找到系统应用数据目录中的 `calset-viewer.log`，其中保留最近运行的记录。日志可能包含本地路径，发送前可按需删去路径中的个人信息。
