# Keysight PNA / PNA-X 校准文件（*.cal）二进制数据结构

本文根据 Keysight N5247B（固件 A.20.82.01）导出的 `calset_101pts.cal` 整理。该样本为四端口全校准，共 101 个频点，范围 10 MHz–1 GHz，文件大小 155,750 字节。下文的数量、取值和排列以该样本为准。跨机型、固件和校准类型的布局需要相应样本验证。

文件使用 Microsoft MFC CArchive 风格的小端序二进制流。七个逻辑区段顺序写入，文件开头没有固定长度的区段偏移表。`unk_*` 表示含义尚未确定的字段。标为不透明的块仅描述样本中已识别的部分。以下 C 风格声明用于表示线上的顺序和长度，读取时不应用编译器的结构体填充或对齐规则。

## 1. 基础原语

| 类型 | 布局 | 说明与样本 |
| --- | --- | --- |
| `uint8` / `uint16` / `uint32` | 1 / 2 / 4 字节 | 无符号整数。`65 00 00 00` → 101 |
| `int16` / `int32` | 2 / 4 字节 | 有符号整数。`19 FC FF FF` → −999 |
| `float32` / `float64` | 4 / 8 字节 | IEEE 754 单精度／双精度。`AD 00 80 3F` → 约 1.0000206 |
| `ComplexFloat32` | `float32 re`，`float32 im` | 共 8 字节，先实部后虚部。示例 `1.000021 + 0.002902j` |
| `ComplexFloat64` | `float64 re`，`float64 im` | 共 16 字节，先实部后虚部。示例 `(50.0, 0.0)` |
| `CStringW` | `FF FE FF` + 长度 + UTF-16LE | 长度按 UTF-16 码元计，不含末尾 NUL。`FF FE FF 0C` 是 `PNA Cal File` 的前缀。`FF FE FF FF D4 02` 表示 724 个码元 |
| `GUID` | 16 字节 | 前三个字段分别为小端 `uint32`、`uint16`、`uint16`，后 8 字节按原顺序。示例 `a1cbab92-2ea7-4d24-a19f-c4b482affaa3` |
| `FILETIME_SWAPPED` | `uint32 dwHigh`，`uint32 dwLow` | 从 1601-01-01 00:00:00 UTC 起的 100 ns 计数。`AE 7C DD 01 2F 9D CA 5B` → `2026-11-25 07:26:59.7141807 UTC` |
| `VARIANT_BSTR` | `uint32 byte_len` + UTF-16LE | Section 6 的 `VT_BSTR`。长度按字节计，没有 `FF FE FF` 前缀。`N5247B` → `0C 00 00 00` + 12 字节 |

所有多字节整数和浮点值使用小端序。`CStringW` 的长度编码如下。

| 三字节标记后的值 | 长度 |
| --- | --- |
| `uint8` < `0xFF` | 该字节即长度 |
| `0xFF` + `uint16` < `0xFFFF` | 后续 `uint16` 即长度 |
| `0xFF` + `0xFFFF` + `uint32` | 后续 `uint32` 即长度 |

读出长度 `n` 后，紧跟 `n × 2` 字节 UTF-16LE 数据。`FILETIME_SWAPPED` 先读高字再读低字，合并为 `ticks = (uint64(dwHigh) << 32) | dwLow`。

## 2. 区段总览

| 区段 | 名称 | 内容 | 样本规模 |
| --- | --- | --- | --- |
| 1 | FileHeader & SchemaTable | 魔数、CalSet 标识、时间戳、子系统版本 | 39 个子系统 |
| 2 | RawMeasurements | 按校准步骤索引的原始标准件迹线 | 46 条 × 101 点 |
| 3 | StimulusAndHardwareConfig | 激励参数、扫频与功率段、SRFI 属性树、衰减器配置 | 5 个基础参数、13 个段及属性树 |
| 4 | MappedMeasurements | 按求解器校准项名映射的标准件迹线 | 46 条 × 101 点 |
| 5 | CalSettingsAndDspSnapshot | 校准设置、激励摘要、RF 开关路径、DSP 窗 | 63 条开关路径、142 点 Tukey 窗 |
| 6 | CalSetMetadata | COM VARIANT 属性 | 9 条 |
| 7 | ErrorTermSet & Footer | 端口参考阻抗、频率轴、误差项、兼容性尾部 | 24 项 × 101 点，矩阵占 38,784 字节 |

## 3. 各区段的布局

### Section 1 · 文件头与子系统版本表

文件偏移 `0x0000` 开始为以下结构。注释中的字段值来自样本。

~~~c
struct SchemaEntry {
    CStringW module_name;
    uint32_t schema_version;
};

struct CalSetHeaderRecord {
    CStringW         name;           // "CalSet_101pts"
    GUID             guid;
    FILETIME_SWAPPED timestamp;
    uint32_t         flags;          // 48 (0x30)
};

struct FileHeader {
    CStringW         magic;          // "PNA Cal File"
    uint32_t         file_version;   // 1
    uint32_t         sub_ver_a;      // 3
    uint32_t         calset_count;  // 原样本为 1，DIQ 样本为 3
    CalSetHeaderRecord calsets[calset_count];
    uint32_t         unk_2;          // 2
    uint32_t         unk_3;          // 1
    uint16_t         unk_4;          // 15 (0x0F)
    uint32_t         schema_count;   // 39
    SchemaEntry      schemas[schema_count];
};
~~~

在已观察的 1.3 文件头中，偏移 `0x24` 的字段是 CalSet 记录数，旧解析器将其命名为 `sub_ver_b`。DIQ 样本的三条记录分别从 `0x28`、`0x6C`、`0xB0` 开始，名称后缀依次为 `001`、`002`、`003`。每条记录各有 GUID、时间戳和标志。记录之后是全局字段，子系统条目数 `39` 位于 `0xFE`，第一条子系统记录从 `0x102` 开始。这些偏移来自该 DIQ 样本，记录名称的长度会影响后续偏移。

样本的子系统名和版本如下，按文件中的顺序列出。跨固件解析后续硬件快照时可参考这些版本号。

~~~text
Allegro(102), Ballade(0), Cello(1), D(6), Daytona(0), DemEE(0),
Distortion2(0), Diva(9), FixtGen(3), Horizons(2), Jupiter(1), Kapaa(1),
Kihei(0), Kihei22(12), Kihei22Banded(0), Mars(2), Mint(44), MintP(9),
Nexus(2), O\Q@(2), PXIFOM(0), PepperMint(2), Piccolo(3), Pioneer(0),
Raichu2(2), SA25(22), Sequoia(0), TDR(1), Talladega(4), TalladegaM4(20),
TdrAnly(0), Totoro2(1), UMS(1), UranusR2(4), VSA(14), Viola(2),
ViolaX(10), VuusxB(2), Waltz(0)
~~~

### Section 2 · 原始标准件测量表

紧接版本表，记录各校准采集步骤的复数迹线。样本中 `outer_size = 14 + inner_size + 6`，`inner_size = header_size + num_points × 8`。

~~~c
struct ComplexFloatTrace {
    uint16_t       flag;              // 1
    uint32_t       outer_size;        // 848 (0x0350)，从下一字段计

    uint16_t       unk_a;             // 1
    uint32_t       unk_b;             // 1
    uint16_t       data_type;         // 2
    uint16_t       unk_c;             // 256 (0x0100)
    uint32_t       inner_size;        // 828 (0x033C)，从下一字段计

    uint32_t       unk_d;             // 1
    uint32_t       unk_e;             // 0
    uint16_t       header_size;       // 20 (0x0014)
    uint16_t       elem_type;         // 3，ComplexFloat32
    uint32_t       dim_count;         // 1
    uint32_t       num_points;        // 101
    ComplexFloat32 data[num_points];  // 808 字节

    uint32_t       trailer_flag;      // 1
    uint16_t       trailer_pad;       // 0
};

struct TraceMapEntry {
    CStringW          key;
    ComplexFloatTrace trace;
};

struct RawMeasurements {
    uint32_t      trace_count;       // 46
    TraceMapEntry entries[trace_count];
};
~~~

| 测量 | 键名模式 | 示例 |
| --- | --- | --- |
| 反射标准件 | `Step:<step>;[State:<state>;]Parameter:S<p><p>` | `Step:1;Parameter:S11`、`Step:1;State:10;Parameter:S22` |
| 直通 S 参数 | `Step:<step>;State:<state>;Parameter:S<i><j>` | 端口对直通步骤中的反射与传输项 |
| 参考接收机比值 | `Step:<step>;State:<state>;Parameter:a<i>/a<j>_<src>` | `Step:1;State:14;Parameter:a1/a2_2` |

### Section 3 · 激励配置、扫频段与 SRFI 属性树

紧接原始测量表。开头是基础激励参数和元信息，其后为扫频／功率／端口频率段、类型化 SRFI 属性树及衰减器状态块。

~~~c
struct ParamPair {
    uint32_t param_id;       // 0=IFBW，1/3=StartFreq，2/4=StopFreq
    double   value;          // 单位 Hz，样本依次为 100000、1e7、1e9、1e7、1e9
};

struct StimulusHeader {
    uint32_t         param_count;    // 5
    ParamPair        params[param_count];
    CStringW         name;           // "CalSet_101pts"
    CStringW         alias;          // "CalSet_101pts"
    CStringW         created_by;     // "Standard"
    CStringW         description;    // ""
    GUID             calset_guid;    // 与 Section 1 一致
    uint8_t          flag_1;         // 1
    FILETIME_SWAPPED timestamp;     // 与 Section 1 一致
    uint8_t          flag_0;         // 0
    uint32_t         unk_one;        // 1
    CStringW         port_config;    // "4P/1-2-3-4"
};

struct SweepSegment {
    uint32_t tag;                   // 29 (0x1D)
    uint16_t ver_a;                 // 1
    uint16_t ver_b;                 // 1
    uint16_t unk_0;                 // 0
    uint32_t enabled;               // 1
    double   start_val;             // 频率（Hz）或功率（dBm）
    double   stop_val;              // 频率（Hz）或功率（dBm）
    uint32_t num_points;            // 101
    uint8_t  mode_flag;
};

struct SRFIPropertyNode {
    uint32_t type_id;
    CStringW cpp_type_name;         // 例如 "class SRFI::GPScalar<double>"
    CStringW prop_name;             // 例如 "PulsePeriod"、"RcvPath0"
    // 其后为按类型解释的 payload
};
~~~

样本的 13 个段由 1 个主频段、6 个端口功率段和 6 个端口频率段组成。其后可见名称为 `UN-NAMED` 的 `SRFI::GPCollection` 节点及其属性树。单独列出的类型优先于范围项。

| `type_id` | 样本中的 payload |
| --- | --- |
| `0x01`、`0x03`、`0x0A–0x14` 中其余已识别枚举 | `int32` 或 4 字节枚举 |
| `0x02` | `CStringW` |
| `0x08` | `uint32 count`，随后是 `count` 个 `SRFIPropertyNode` |
| `0x0C` | 1 字节布尔值 |
| `0x0F` | 已删除占位，0 字节 |
| `0x10` | `float64` |
| `0x12` | `PNAUtility::InputArray`，变长数组 |

样本中的属性包括 `PulsePeriod = 0.001 s`、`PulseWidth1..4 = 0.0001 s`、`PrimaryRangeFrequencies`、`UserChannelNumber`、`RcvPath0..7`、`RcvSwtc0..7`、`NBFIFAtn0..7`、`IFGate0..7` 和 `RcvIFGainA..D/R1..R4`。属性树之后的接收机与端口衰减器配置按不透明块处理，其布局与机型和选件相关。

### Section 4 · 映射后的标准件测量表

沿用 Section 2 的 `uint32 count` + `TraceMapEntry[count]` 布局。键名由采集步骤名映射为求解器中的校准项名。四端口样本的 46 条复数数据与 Section 2 中对应迹线的 payload 逐字节一致。另一个 271,977 字节的单端口样本中，Section 2 的原始迹线数为 0，映射表从 `0x2045E` 开始。计数字段为 3，随后三个键依次为 `SA(1,1)`（`0x20462`）、`SB(1,1)`（`0x2A0E2`）和 `SC(1,1)`（`0x33D62`）。

| 类别 | 样本键名 |
| --- | --- |
| 单端口标准件 | `SA_0(p,p)`、`SA_1(p,p)`、`SA_2(p,p)`、`SB_0(p,p)`、`SB_1(p,p)`、`SC_0(p,p)`、`SC_1(p,p)`，`p = 1..4` |
| 单端口样本的无编号键 | `SA(1,1)`、`SB(1,1)`、`SC(1,1)` |
| 端口对直通 | `(1,2)`、`(1,3)`、`(1,4)` 各有四个 S 参数。例如 `Thru_12(1,1)`、`Thru_12(2,1)`、`Thru_12(1,2)`、`Thru_12(2,2)` |
| 参考接收机比值 | `Thru_RefRatio(1,2)`、`(1,3)`、`(1,4)`、`(2,1)`、`(3,1)`、`(4,1)` |

### Section 5 · 校准设置与 RF／DSP 快照

在样本中，`Cal Settings File` 锚点前有一个值为 4 的 `uint32` 字段。以下布局描述校准设置头部。

~~~c
struct PortCalKitEntry {
    uint32_t entry_prefix;        // 0
    uint32_t port_index;          // 1、2、3、4
    CStringW connector_type;      // "1.85 mm male"
    CStringW calkit_name;         // "SimECal4 User 2 ECal SN00000000"
};

struct CalSettingsHeader {
    uint32_t        section_id;   // 4
    CStringW        marker;       // "Cal Settings File"
    uint16_t        ver;          // 1
    CStringW        empty_str;    // ""
    uint32_t        unk_451;      // 1105 (0x0451)
    uint32_t        unk_1;        // 1
    uint32_t        unk_0;        // 0
    CStringW        timestamp_utc;// "2026-11-25 07:26:59"
    uint32_t        unk_table_1;   // 1
    uint32_t        unk_table_0;   // 0
    uint32_t        port_count;    // 4
    PortCalKitEntry port_kits[port_count];
};
~~~

校准件表之后，在样本中依次可见以下内容。

1. Thru 端口对 `(1,2)`、`(1,3)`、`(1,4)`，3 条连接提示以及 ECal 端口映射（1 → A，2／3／4 → B）。
2. `Std CoreStimulus` 锚点、`uint16(1)` 与长 `CStringW`，后者包含 724 个 UTF-16 码元的激励摘要。
3. 与机型、选件和子系统版本相关的硬件／DSP 快照。样本中观察到以下结构。

   - Analog I/O（`AO1`、`AO2`、`AI1`、`AI2`、`AIG`、`AOS1`、`AOS2`）。
   - 3 组脉冲／IF 模块和 6 组端口接收机模块（`R1,1` 至 `R1,6`）。
   - RF 开关路径映射，`uint32 count = 63`，随后是 `count` 对 `CStringW key`、`CStringW val`。
   - LO／混频器分段（`INPUT`、`OUTPUT`、`LO1`、`LO2`）。
   - DSP IF 滤波器 Tukey 窗，`uint32 len = 142`，随后是 `uint32 coef[142]`，峰值 131071（`0x1FFFF`）。
   - 9 个窗描述符，可见 `TUKEY`、`RECT`、`PWIN`、`GAUSSWIN`、`COEF`、`NO ERROR`。
   - `TestSetMap`（`internal`）和 VNA Security Level（`None`）。

### Section 6 · CalSet 元数据

以 `uint32 entry_count` 开始，随后是对应数量的条目。每个条目为 `CStringW key`、两个相同的 `uint16 VARTYPE` 和由该类型决定的值。

| VARTYPE | 值布局 |
| --- | --- |
| `0x0008`（VT_BSTR） | `uint32 byte_len`，随后是 `byte_len` 字节 UTF-16LE |
| `0x0002`（VT_I2） | `int16` |
| `0x0003`（VT_I4） | `int32` |
| `0x0004`（VT_R4） | `float32` |
| `0x0005`（VT_R8） | `float64` |
| `0x000B`（VT_BOOL） | `int16`，零为假，非零为真 |

样本解码结果如下。

| 键 | 类型 | 值 |
| --- | --- | --- |
| `Cal Acquisition Information` | VT_BSTR | `Cal Acquisition session index 0:Full 4 Port(1,2,3,4) on Nov 25, 2026 at 3:26:59 PM--...` |
| `Created By` | VT_BSTR | `Standard` |
| `Firmware Revision` | VT_BSTR | `A.20.82.01` |
| `Model Number` | VT_BSTR | `N5247B` |
| `Serial Number` | VT_BSTR | `SM78030557` |
| `USEDPRIMARY` | VT_I2 | `0` |
| `UnknownThruDelaySec_1_2` | VT_R4 | `7.696542e-10 s` |
| `UnknownThruDelaySec_1_3` | VT_R4 | `-4.630522e-10 s` |
| `UnknownThruDelaySec_1_4` | VT_R4 | `-1.253752e-09 s` |

### Section 7 · 校准误差项与兼容性尾部

误差项区段从 `00 00 00 00 EE 03 00 00` 开始。矩阵按误差项优先、每项内部按频点顺序排列。样本矩阵占 `24 × 101 × 16 = 38,784` 字节。

~~~c
struct ErrorTermSet {
    uint32_t       pad_zero;        // 0
    uint32_t       magic;           // 1006 (0x03EE)
    uint32_t       ver_a;           // 1
    uint32_t       ver_b;           // 1

    uint32_t       num_ports;       // 4
    uint32_t       port_ids[num_ports];        // 1、2、3、4
    uint32_t       num_z0;          // 4
    uint32_t       unk_z0_prefix;   // 1
    ComplexFloat64 port_z0[num_z0];            // 各端口 50.0 + 0.0j Ω

    uint32_t       num_freqs;       // 101
    double         freqs_ghz[num_freqs];       // 0.01–1.00 GHz，步进 0.0099 GHz

    uint32_t       unk_one_1;       // 1
    uint32_t       unk_one_2;       // 1
    uint32_t       unk_zero;        // 0

    uint32_t       num_error_terms; // 24
    uint32_t       num_points;      // 101
    ComplexFloat64 error_terms[num_error_terms][num_points];
};
~~~

- `error_terms[0]` 在所有频点均为 `1.0 + 0.0j`，样本分析将其视为归一化参考项。
- 其余项呈现与方向性、源匹配和负载匹配（约 10⁻⁴–10⁻³）、反射跟踪（接近 ±1）以及传输跟踪相符的特征。一条与 Thru S12 相位共轭旋转相关的迹线幅度约 0.904–0.915，前四个相位约为 −15.15°、−30.03°、−44.84°、−59.51°。这些特征可辅助识别误差项，样本尚未给出完整的项编号映射或误差模型分组。
- 样本中的非单位误差项按 `ComplexFloat64` 存储，观测到尾数最低 3 字节为零。该现象与由 `float32` 扩展为 `float64` 相容。要判断数值能否无损表示为 `float32`，可检查转换为 `float32` 后再转回 `float64` 是否逐值相等。

样本矩阵之后的兼容性尾部依次可见以下字段。

| 顺序 | 布局与样本值 |
| --- | --- |
| 1 | `uint8(1)`、`uint32(1006)`、`uint32(0xFFFFFFFF)`、`uint32(1006)` |
| 2 | `double[num_ports]` 校准功率（每端口 −5.0 dBm）、`uint32(num_points)`、零值填充块 |
| 3 | 机械开关状态表，`uint32(10)` 后接 10 个 `CStringW` |
| 4 | 激励兼容性检查表，`uint32(1)`、`uint32(24)` 后接 24 个 `CStringW`，12 组检查标签各出现两次 |
| 5 | 端口尾表，`uint32(4)` + `uint32[4]`（1、2、3、4），`uint32(4)` + `int32[4]`（四个 −999，表示未指定外部映射），`uint32(4)` + 4 个空 `CStringW` |

## 4. 跨机型与固件的解析策略

Section 3 后半段的硬件属性与衰减器配置，以及 Section 5 后半段的 RF／DSP 快照，会随子系统版本、机型和选件变化。顺序读取 Section 1、Section 2 和 Section 3 中的已知字段，可以建立后续搜索的起点。对不透明区段，按结构锚点重新定位并核对候选区段的长度、计数和数据类型。

| 区段 | 仓库中 `parse_pna_cal.py` 的实现 |
| --- | --- |
| 1、2 | 从文件开头顺序解析文件头、子系统版本表和原始迹线 |
| 3 | 读取基础参数、元信息和端口配置，扫频段、SRFI 属性树与衰减器状态块仅在本文记录 |
| 4 | 从 `SA`、`SB`、`SC`（含带下划线编号的变体）以及 `Thru_` 系列键定位候选，向前读取计数并校验迹线表 |
| 5 | 从 `Cal Settings File` 定位校准设置头部和端口校准件，另搜索 `Std CoreStimulus` 文本。RF／DSP 快照仅在本文记录 |
| 6 | 从 `Cal Acquisition Information`、`Created By` 或 `Model Number` 候选键向前读取计数，尝试解码 VARIANT 表 |
| 7 | 从元数据末尾或 Section 3 头部末尾搜索 `(0, 1006, 1, 1)` 签名，检查端口数量、Z0 数组长度与频点矩阵维度。兼容性尾部仅在本文记录 |

在仓库根目录运行以下命令，输出摘要并导出能解析到的数据。命令行解析器只依赖 Python 标准库。

~~~sh
python parse_pna_cal.py calset_101pts.cal --export-dir ./cal_out
~~~

可参阅[解析器源码](../parse_pna_cal.py)和[查看器使用说明](../README.md)。
