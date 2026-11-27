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

## 单端口校准数据

在已识别的单端口格式中，左侧 **误差项** 节点按文件内的名称显示 `Directivity`、`ReflectionTracking` 和 `SourceMatch`。全量导出生成 `named_error_terms.csv`。记录缺少可识别的频率轴时，曲线和数据表保留点索引，CSV 的 `freq_hz` 留空。

在 VNA Simulator 中加载同一 CalSet 后，可以按名称验证这三个误差项。

```powershell
uv run --locked --with pyvisa python .\tools\export_error_term.py --verify-cal "$env:USERPROFILE\Desktop\CalSet\calset_1port.cal"
```

`[Named error term verification]` 报告每项的复数值匹配结果和最大残差。含频率轴的迹线先核对频率。对于文件未保存可识别频率轴的迹线，报告 `point order only`，表示按文件中的点序比对复数值。

如果单端口文件的 **映射标准件测量** 仍为空，可以输出原始测量键名，以及其后的复数记录候选：

```sh
uv run --locked python parse_pna_cal.py "your_calset.cal" --inspect-mapped-measurements
```

## 矩阵误差项名称

### 用 VNA Simulator 验证矩阵行顺序

在 VNA Simulator 中 Recall 要验证的 CalSet 后，在运行模拟器的 Windows 电脑上执行。

```powershell
uv run --locked --with pyvisa python .\tools\export_error_term.py --verify-cal "$env:USERPROFILE\Desktop\CalSet\calset_2port.cal"
```

默认连接 `TCPIP0::127.0.0.1::hislip0::INSTR`，可用 `--resource` 指定其他 VISA 地址。模拟器中使用了不同的 CalSet 名称时，可追加 `--calset "实际名称"`，或先用 `--list-calsets` 查询名称。该模式需要启用模拟器的远程控制。

脚本核对校准端口和逐项频率轴，再比较全部复数点。在 `[Matrix row mapping]` 中报告唯一匹配、歧义和未匹配的行。请同时保留 `[Simulator terms]` 和 `Unique matches`，便于区分频率不匹配和复数值不匹配。验证只输出报告，查询数据时临时使用 ASCII 传输并恢复原设置。

`[CalSet identity]` 记录文件大小、SHA-256 和原 GUID，并查询原 GUID 是否存在于模拟器的 CalSet 存储。有未确认的矩阵行时，`[Matrix mismatch diagnostics]` 还会给出不同数据布局下的匹配数量，以及原布局下最近矩阵行的复数残差。残差大小只能用来定位差异。
`[Sign and reflection checks]` 进一步比较整条曲线取负后的结果，并验证矩阵行是否符合一端口反射修正公式的系数。输出用于验证存储模型。

### 用单项修改验证存储关系

当逐点匹配只确认部分行时，可以在模拟器中做一次差分实验。以下命令会创建两个新 CalSet 副本，在其中一个副本的 `Directivity(1,1)` 上加入三个复数标记，并用仪器读回值核对标记。请在测试用的 Simulator 会话中运行。

```powershell
uv run --locked --with pyvisa python .\tools\probe_cal_matrix.py prepare --calset CalSet_2Port --term "Directivity(1,1)"
```

按脚本打印的名称，在模拟器中分别将 `ProbeBase_...` 和 `ProbeEdit_...` 应用到测试通道，再各导出一个 `.cal` 文件。保存 `.cal` 会包含当前通道在用的 CalSet，所以两次导出各只应用对应的副本。把基准文件和标记文件分别保存为 `baseline.cal` 和 `modified.cal`，然后执行离线比较，`--original` 指向最初的校准文件。

```powershell
uv run --locked python .\tools\probe_cal_matrix.py compare .\baseline.cal .\modified.cal --original "$env:USERPROFILE\Desktop\CalSet\calset_2port.cal"
```

`[Matrix probe]` 报告变化的矩阵行和标记是否一致。只有一行完整重现读回的标记时，脚本才报告直接存储对应关系。`prepare` 默认生成 `calset_probe.json`，重复实验时可为两条命令指定相同的新 `--manifest` 路径。

### 为当前文件保存名称

如果两端口或四端口文件的矩阵只显示 `Term 0` 等编号，可在能连接 PNA 的电脑上将导出文件与仪器上的同一 CalSet 逐点匹配：

```sh
uv run --locked --with pyvisa python tools/export_error_term.py --label-cal "your_calset.cal"
```

默认按文件内的 CalSet 名称查询仪器，必要时用 `--calset` 指定仪器上的名称。匹配成功会在 .cal 旁生成 `your_calset.cal.terms.json`。重新打开 .cal 时，Viewer 会在校验文件 SHA-256 后读取匹配到的名称。同值、歧义或无法匹配的迹线继续显示编号。全量导出的 `summary.json` 同时记录已验证的编号与名称。

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

## 误差项识别诊断

如果左侧缺少 **误差项矩阵** 或 **误差项**，或矩阵只显示 `Term 0` 等编号，可以在仓库目录运行离线诊断。

```sh
uv run --locked python parse_pna_cal.py your_calset.cal --inspect-error-terms
```

命令报告误差项数量、候选区段偏移、校验结果和已识别的具名记录。如果文件包含误差项矩阵，还会输出矩阵边界、尾部字节及矩阵外的 ASCII 和 UTF-16LE 名称候选。候选文字只用于定位，矩阵行与名称的对应关系需要由文件结构确认。

## 导入日志

导入失败时，窗口底部会自动展开 **导入日志**。点击 **复制日志**，即可粘贴本次运行的诊断记录。也可通过 **查看 → 导入日志** 手动打开面板。

日志记录文件名、读取字节数、解析区段与偏移、异常堆栈和导入结果。点击 **打开日志目录** 可找到系统应用数据目录中的 `calset-viewer.log`，其中保留最近运行的记录。日志可能包含本地路径，发送前可按需删去路径中的个人信息。
