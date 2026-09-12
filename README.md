# MRI 软骨可视化平台

局域网内的膝关节 MRI + 分割标注浏览器。四窗口（横断/冠状/矢状 + 3D），按部位着色与显隐，
面向「分割模型跑完之后要能一眼看结果」这个目标设计。**可在完全不联外网的 Ubuntu 22.04 上部署。**

---

## 快速开始（开发机）

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r server/requirements.in
pip install -e server

export MRIVIEWER_CONFIG=/path/to/config.toml     # 参考 deploy/config.example.toml
mrictl init                 # 建库 + 载入标签集
mrictl scan -d ds0826       # 一级扫描：建患者/检查/序列树
mrictl materialize -d ds0826   # 二级：DICOM -> 体数据缓存 + 标签图
mrictl mesh                 # 预计算 3D 表面
mrictl serve                # http://0.0.0.0:8080

cd web && npm ci && npm run build     # 前端产物 -> web/dist，由后端直接托管
```

`mrictl stats` 看进度，`mrictl doctor` 检查依赖和数据路径。

---

## 这个平台解决的三个真问题

### 1. 各向异性 10:1

膝关节数据是 **0.2917 × 0.2917 × 3.0 mm**。矢状位采了 30 层，冠状/横断方向就只有 30 个采样点。

- **2D 重建**：不做服务端重采样。Cornerstone 用真实世界间距建三维纹理，重建几何是**准确的**，
  只是层方向糊——因为数据本来就只有这么多。界面上如实标注「重建视图 · 层厚 3.0 mm」，
  并标出哪一个是采集平面，而不是假装三个平面等价。
- **3D 表面**：直接对二值标签跑 marching cubes 会得到 3 mm 的硬台阶。
  管线是：`AntiAliasBinary` 演化出平滑水平集 → 线性重采样到 0.5 mm 各向同性
  → `vtkFlyingEdges3D` 取零等值面 → windowed-sinc 平滑 → 按三角形预算减面。

  **这个选择是量出来的，不是拍脑袋的。** 用「法向与层轴夹角 <10° 的表面积占比」
  作为台阶指标（越低越平滑），在 case 1 上对比两种方案：

  | 标签 | SignedMaurerDistanceMap | AntiAliasBinary |
  |---|---|---|
  | 4 股骨内侧软骨 | 5.6% | **1.4%** |
  | 5 股骨外侧软骨 | 3.4% | **1.2%** |
  | 8 髌骨软骨 | 1.5% | **0.4%** |
  | 1 股骨 | 30.6% | **24.3%**（大部分是真实解剖形状） |

  一开始用的是符号距离场，实测**更差**：距离值量化到体素中心，3 mm 网格上零等值面
  仍然贴着层平面。软骨的台阶因此下降到原来的 1/4 左右。

### 2. 方向/翻转的正确性

NIfTI 是 RAS+，DICOM 和 Cornerstone 是 LPS。搞错一个符号，软骨叠加就会左右镜像——
而镜像的膝关节看上去**足够合理**，能骗过随手一瞥。

因此 `seg/nifti_io.py` 在入库时就把「NIfTI 数组轴 → 规范 (k,j,i)」的轴置换 + 翻转算出来存进
`segmentation.ingest_transform_json`，保存修正时套用精确逆变换，并且**复用源文件的 affine 和
header 原样写回**（不从自己的 origin/direction 重建——那正是符号错误的藏身之处）。

`tests/test_orientation_roundtrip.py` 是本项目最有价值的一个测试：
- 合成一个各轴各向都不对称的体模，穷举 NIfTI affine 能表达的全部 **48 种轴置换/翻转组合**，
  断言逐字节还原；
- 对全部 **24 例真实标注**，断言推导出的变换等于人工核对过的 `transpose(2,0,1)`、
  几何残差 < 1e-3 mm、编辑→保存→重载逐字节一致；
- 断言把 A 病例的标签配给 B 病例会被**拒绝**而不是静默显示。

### 3. 离线部署

最终前端产物是**纯 JS，零 WASM，零外部主机**。关键在于不使用
`@cornerstonejs/dicom-image-loader`（体数据由后端转好直传，浏览器根本不解析 DICOM），
连带去掉了 JPEG/JPEG2000 的 WASM 编解码器、`dicom-parser` 的 CommonJS 补丁；
3D 网格用自定义 `MRIVMESH`（就是两个扁平数组）而不是 glTF/Draco，也就不需要 Draco 解码器。

- 构建期：`packaging/verify_no_cdn.sh` 检查 HTML 外链、已知 CDN 主机、WASM、绝对 URL worker。
- 运行期：`Content-Security-Policy: default-src 'self'` 兜底。
- 虚拟环境用 `virtualenv.pyz` 而不是 `python3 -m venv`——Ubuntu 把 `ensurepip` 剥离到
  `python3.10-venv` 这个 deb 包里，断网机器装不上。

---

## 软骨定量测量与 AI 报告

阅片页右侧栏（分割标签下方）可就地生成中文诊断报告；「查看详细报告」弹出指标看板，
把 22 个亚区的厚度、体积、内外侧对称性画成图。

### 测量口径

沿骨-软骨界面法线投射测厚（CartiMorph / Chondrometrics 口径），亚区划分用 Eckstein 方案
（选它的唯一理由：只有这套能查到公开的分性别 mean ± SD 参考值），并挂 MOAKS 名称。

关键实现在 `server/src/mriviewer/morph/`：

| 文件 | 作用 |
|---|---|
| `frame.py` | 由标签自身推出解剖坐标系与侧别 |
| `geometry.py` | 骨与软骨落在**同一**各向同性网格；测量用不减面的表面 |
| `thickness.py` | 法线投射测厚 + 最近邻交叉校验 |
| `parcellation.py` | 亚区划分（股骨按自身角度跨度比例，胫骨中央椭圆 + 四象限） |
| `compute.py` | 指标树 + QC + 逐亚区可信度 |

### ⚠ 这个平台测不准什么，以及为什么

体模实测（`tests/test_thickness_phantom.py` 固化了这些数字）：

- 各向同性数据上管线**无偏**，误差 **+0.001 mm**；
- 但在 3 mm 层厚的矢状位 2D 数据上，**即使在「可靠区」也系统性低估 7-18%**；
- 且偏差随真实厚度**非单调**（1.0 mm 时 −0.14，2.0 mm 时 −0.35，3.0 mm 时 +0.23），
  **无法用固定系数校正**。

所以：**绝对厚度不可直接与文献参考值比较**。可靠的是同一膝关节的**内外侧对比**、
同协议的**纵向随访**、以及厚度的**空间分布**。参考值（Framingham KL0）一律标
`applicability: indirect`，图上画成斜纹，表示量级参照而非诊断阈值。

逐顶点还会算 `effectiveResolutionMm` —— 测厚方向上原始数据的采样间隔。法线落在矢状面内时
约 0.3 mm，指向内外侧时等于层厚 3 mm。哪些区域可信是**算出来的**，不是声称的。

**内外侧标签互换是硬失败**，不出指标：镜像后的报告看起来完全合理，比不出报告危险得多。

### AI 报告的防编造机制

**所有数字由 Python 算好传入，模型只写文字。** 返回后四道校验（`ai/guardrail.py`）：

1. **数值回溯** —— 正文里每个数字都必须能在载荷中找到（容差 0.015 mm）。这条拦的是最危险的
   失败：一段读起来完全合理、数字却是编出来的文字。
2. **禁用词** —— 半月板 / 韧带 / 骨髓水肿 / 积液 / 撕裂 / 骨赘…… 标签 1-8 推不出这些结构。
3. **亚区代码白名单**；4. **四段齐全**。

失败重试一次，仍失败整份拒收并回退到内置模板。**LLM 是增强，不是依赖**：
`ai.enabled = false` 时 `ai/fallback.py` 用同样的数字确定性地拼出同样四段报告，断网机照常可用。

### 隐私

载荷由白名单**逐字段构造**（`ai/payload.py`），绝不遍历数据库行；
`assert_deidentified()` 在**建立连接之前**扫描并抛错。不含姓名、病历号、任何 ID、
检查号、UID、日期、文件路径、数据集名、放射科原始报告或任何像素数据；年龄只保留十年段。
界面上「查看实际发送给模型的内容」可逐字审计。

API Key 只写入不读出：存在服务器上权限 0600 的文件里，任何接口都不回传。
`ai.enabled` 开功能，`ai.allow_egress` 才允许连非本机地址，且在客户端解析主机后**强制执行**。
断网部署把 `base_url` 指向本地模型（Ollama / vLLM）、保持 `allow_egress = false` 即可。

> **注意**：本平台自身没有登录认证，设计前提是部署在受控局域网内。
> 「AI 管理」面板可在界面上修改接口地址与外网开关，意味着能打开页面的人都能改。

---

## 结构化膝关节报告（章节框架）

`#/report/<segId>`。一份文档、固定章节、三种视图（网页 / 打印 / 编辑），阅片页右侧栏
只有**一个**「诊断报告」卡片：一个按钮依次跑完软骨形态学测量 → 软骨报告 → 各章节，
生成过程按步骤流式回报（SSE），完成后卡片上只有「重新生成」和「查看详细报告」两个按钮。

- **框架在 `server/reports/knee_zh_v1.yaml`**：章节顺序取自本院 157 份样例报告的固定套话
  （骨 → 软骨 → 积液/滑膜 → 半月板 → 韧带肌腱 → 囊肿 → 软组织 → 印象 → 建议 → 定量附录）。
  每章声明数据来源：`computed`（本平台算法）/ `model`（影像模型）/ `radiologist` / `composed` / `pending`。
- **`pending` 章节绝不产生正文**，只显示「本次未评估（尚未接入相应模型）」。若该病例有放射科原始报告，
  其相关句子按章节附上，标签固定为「放射科报告」——引用，不冒充。接入一个新模型 = 在
  `report/sources.py` 注册一个 resolver + 改 YAML 的 `source`。
- **所有数字来自计算**（`facts`/`grades` 取自形态学结果，不取自正文）；AI 只解读软骨章，禁用词按章节配置。
- **医生可改文字、数字、分级**（`report_document.overrides_json`）：每处修改保留原值、修改人、时间，
  打印件上显示「医师修改（原值 X）」；重新生成后修改保留并标「请复核」；每次修改进只追加的 `report_edit_log`。
  复核通过时冻结 `signed_json`；重新生成一律重置复核状态。
- **放射科报告导入**：`reports_file` 指向 `knee_MR_report.xlsx`（`序号` 与 DICOM 目录同编号；88/157 行的
  影像描述与诊断意见相同，已标记）。含姓名列的文件会被拒绝。

### Outerbridge（厚度推导，非信号）

`morph/outerbridge.py` + `morph/coverage.py`。II/III 由**局灶**厚度缺失判定，基线是周围约 10 mm 内软骨的
平均厚度（两遍估计，病灶不参与自己的基线）；软骨板边缘 3 mm 内不评估；只跨一层的斑块不报；
IV 级来自被软骨包围（包围度 ≥ 0.8）的裸露骨面。**I 级需信号信息，本方法不评估，每份报告都写明。**
阈值在 `refs/knee_cartilage_reference_v1.yaml` 的 `outerbridge:` 块，改动即产生新的形态学记录。

8 例标注膝的结果：7 例全部 0 级，1 例两处 IV；这些病例的放射科报告均未提及软骨病变。
样例报告里没有任何正式分级，因此这一层**无法用样例监督**，是新增的、须医生复核的输出。

### PDF

打印视图是白底 A4 栏，浏览器 Ctrl+P 即可另存 PDF。服务端 PDF（`GET /api/v1/segmentations/{id}/report.pdf`）
用 Playwright 的无头 Chromium 渲染同一打印视图，页眉去标识化、页脚免责声明、页码。
`mrictl doctor` 显示 playwright / Chromium / 中文字体状态，`mrictl pdf-selftest` 渲染一段中文供人工核对字形。
离线包由 `packaging/build_bundle.sh` 在联网构建机上带上 `chromium-headless-shell`、其共享库 .deb 与 `fonts-noto-cjk`。

---

## 架构

```
浏览器  ── /api/v1/series/{id}/volume.json   几何 + Cornerstone metadata
        └─ /api/v1/series/{id}/volume.raw    int16/uint16 原始体数据
                                             （落盘就是 gzip，Content-Encoding: gzip 直出，
                                              浏览器原生解压，服务端零压缩开销）
        ── /api/v1/segmentations/{id}/labelmap.raw   uint8 标签图，同几何
        └─ /api/v1/segmentations/{id}/mesh/{n}.bin   MRIVMESH 表面
```

一个膝关节序列 480×480×30 uint16 = 13.8 MB，gzip 后 8 MB，局域网 1 秒内；
8 个标签的 3D 表面合计约 1.3 MB。

### 分级扫描

整块硬盘约 17 万个 DICOM 文件，全量读头要几小时，所以分两级：

| 级别 | 做什么 | 代价 |
|---|---|---|
| tier 1 `mrictl scan` | 每个 series 目录一次 `scandir` + **只读首/中/末三个文件的头** | ~3 次读/序列 |
| tier 2 `mrictl materialize` | 读全部切片、排序、拼体数据、落 gzip 缓存 | 按需，或后台批量 |

tier 2 也会在浏览器第一次打开该序列时自动触发。两级都按
`(path, dir_mtime, entry_count, total_size)` 记账，重跑跳过未变的，`kill -9` 最多丢一个序列。

### 两条硬规则（都是被真实数据教出来的）

1. **不看扩展名。** `Bone Scan/` 在同一个 UID 命名的文件夹里混放了 `.dcm` 切片和
   **非 DICOM** 的 Interfile 伴随文件（`.A00`/`.I00`）以及 `.dat`/`.jpg`。
   唯一可靠的判据是 offset 128 处的 `DICM` 魔数——一次 132 字节的读取。
   （实测一个患者目录：291 个 `.dcm` 全部通过，4 个 `.A00`/`.I00` 全部被正确拒绝。）
2. **不用 SOP Class 白名单找影像。** 某个普通的 PDW 影像序列目录
   里混着一个 Philips 的 Raw Data Storage 对象（`1.2.840.10008.5.1.4.1.1.66`，3 KB）。
   普适判据是：**同时具备 `Rows`、`Columns`、`BitsAllocated` 才是影像实例**——
   那个对象三个都没有。在此之上再用一个 denylist 排除 Presentation State / SR。

---

## 切换病例时的行为

**切哪个就显示哪个。** 每次切换序列都会先把上一例的分割彻底卸掉
（`clearSegmentations()`：从四个视口移除 representation，再从 Cornerstone 状态里删掉），
然后才加载新的。打开一个**没有标注**的序列时，2D 叠加和 3D 窗口都会是空的，
右侧显示「该序列没有分割标注」，「3D 表面」按钮置灰。

这不是外观问题：这个平台就是用来核对分割结果的，
如果上一位患者的软骨还留在画面上，那是**给出了错误答案**。
`tests/browser_switch_check.mjs` 逐像素守着这条规则。

缓存不清——体数据、标签图、3D 网格都留在浏览器缓存里，
所以切回看过的病例是秒开，不重新下载。

加载时视口上方居中显示进度条，分三段：
`影像体数据 N / 13.8 MB` → `分割标注 N / 6.9 MB` → `3D 表面 n/8`。

---

## 接入新的分割结果（给日后的算法实验留的口子）

往 `predictions_roots` 里丢文件即可，**不需要改代码**：

```
<predictions_root>/<model>/<version>/<series_uid>/<labelset>.nii.gz
```

入库时会校验维度和 affine 与目标序列一致，不一致直接拒绝并在界面上报错。
页面上它会变成分割下拉里的一个新版本，可与「原始标注」并排切换对比。

---

## 数据集适配器

| adapter | 数据集 | 布局 |
|---|---|---|
| `ds0826` | 0826 软骨标注集 | `dicom/<N>/<series>/` + `segmentation label/<N>_*.nii` + `patient list.xlsx` |
| `changzheng` | 长征膝关节 MR，2485 例 | `P####/<series>/` + `reports_deidentified.csv` |
| `fspdw` | fsPDW，893 例 | `<姓名>/<时间戳>/<序列号>/` |
| `copd_nifti` | COPD 胸部 CT | NIfTI 原生；逐结构二值 mask 在入库时合并成单个多值标签图 |
| `bonescan` | 核医学骨扫描 | 任意嵌套，靠 DICM 魔数发现（含 SPECT/CT 与平面显像）；患者在第 3 层，用 `patient_depth` 指定 |

---

## 已知数据问题

- **0826 case 1 的内外侧与其余 23 例相反。** 标签含义是平台从 24 例标注反推确认的
  （见 `labelsets/knee_cartilage_0826_v1.yaml` 的注释）：24/24 例满足
  「4/5 在关节线之上且上下跨度 ~38 mm，6/7 在关节线之下且跨度 ~12 mm」，
  且 `{4,6}` 与 `{5,7}` 在内外侧方向各自成对。但内外侧的归属，
  **23 例是 `{4,6}`=内侧，只有 case 1 相反**。训练前需要人工复核 case 1。
- **0826 里 24 例标注中有 2 例是 JPEG Lossless**（其余 22 例是未压缩 Explicit VR LE），
  所以 `pylibjpeg` + `pylibjpeg-libjpeg` 是硬依赖，不是可选项。
- **COPD 体数据 768×768×320 int16 ≈ 377 MB**，浏览器收不下，
  会自动生成降采样投递变体（`browser_volume_budget_mb`）。

---

## 测试

```bash
cd server && python -m pytest tests/ -q        # 160 项：几何、体模、分区、平滑、去标识化、护栏、报告框架、分级
node tests/integration_check.mjs               # 对着运行中的服务跑完整数据链路
node tests/browser_check.mjs                   # 无头 Chromium，逐像素验证四窗口
node tests/browser_switch_check.mjs            # 病例切换：ID 冲突、残留清空
node tests/metrics_check.mjs                   # 指标看板
node tests/report_flow_check.mjs               # 两段侧栏、弹窗、章节框架、AI 管理
node tests/report_views_check.mjs              # 打印视图白底 A4、编辑覆盖、服务端 PDF
bash packaging/verify_no_cdn.sh web/dist       # 离线校验：不得有任何外部请求
```

浏览器测试用 `MRIV_BASE` 或第一个参数指定服务地址，默认 `http://127.0.0.1:8080`。
需要真实数据的扫描器回归从 `MRIV_TEST_SERIES_DIR` / `MRIV_TEST_BONE_DIR` 读路径，
未设置则跳过 —— 源盘上的目录名带患者姓名/性别/年龄，不进版本库。

值得单独说的是 `test_ai_safety.py`：它用**故意投毒**的载荷测防护 ——
9 种带姓名 / UID / 日期 / 路径 / 手机号的输入必须在联网前抛错，编造的数字和
未分割的结构必须被拒收。只喂干净数据的测试证明不了任何防护。

---

## Cornerstone3D 的静默失败（都写进了代码注释）

这些坑的共同点是**不报错**——没有异常、没有 console.error，画面就是不对：

1. `xmlbuilder2`（vtk.js 依赖）继承 Node 的 `EventEmitter`，Vite externalize node 内置模块后
   变成 `class extends undefined`，整个 bundle 在 React 挂载前就死了。
   → 给 `events`/`url` 加浏览器 polyfill 别名。
2. 自定义 loader 里调 `createLocalVolume` 后**不能**再调 `volume.load()`——
   那是流式体数据的路径，会去 image loader 里找 `mriv:` scheme。
3. `addSegmentations` 若不显式给 `config.segments`，Cornerstone 只注册 **1 个** segment，
   2–8 号标签在它的状态里根本不存在，逐标签显隐/上色全部静默失效。
4. labelmap 的 `FrameOfReferenceUID` **必须等于**影像体数据的，否则
   `isSegmentationOverlayCompatible` 判定不兼容，只 `console.warn` 一句就跳过。
5. **labelmap 必须被设为该视口的 active segmentation**：`renderInactiveSegmentations`
   默认 `false`，非 active 的 labelmap **根本不画**——representation 和 actor 都在、都正确，
   就是不出现。
6. `renderingEngine.resize(immediate, keepCamera)` 的第二个参数传 `false` 会丢掉相机，
   之后任何布局变化（进度条出现、窗口拖动）都会让 2D 视口变黑。传 `true`。
7. `resetCamera()` 不能紧跟在 `setVolumesForViewports` 之后调用——
   此时 actor 的包围盒可能还不存在，相机会指向虚空。等两帧再重置。
   这个 bug 只在**没有分割**的序列上暴露：有分割时，后续挂载 labelmap 的额外渲染
   碰巧把相机修好了。
