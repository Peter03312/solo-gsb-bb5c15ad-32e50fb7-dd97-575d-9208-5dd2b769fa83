# 陶片拼接系统

面向文物修复人员的陶片拼接 Web 应用：录入毫米制闭合轮廓，基于对应边缘标记
做纯平移 + 旋转的刚体配准，支持预览、采纳、撤销与连通组管理。

## 功能

- **碎片录入**：画布点击或粘贴 JSON 录入毫米制闭合轮廓；自交轮廓整次拒绝。
- **候选拼接**：两片各至少 2 处对应边缘标记（数量一致、同片不重合）。
- **预览**：仅经平移 + 旋转的组合，显示各片位置 (x, y)、角度 (°)、每对
  对应点误差及冲突原因，不落库。
- **采纳校验**（任一不满足即整次拒绝，原拼接组不变）：
  - 同组对应点误差均 ≤ 1 mm；
  - 同组陶片轮廓内部不重叠（允许共边/共点）；
  - 合并两组时整体移动其中一组，组内相对位姿不变；
  - 同组内新增关系（闭环）必须符合已有位姿。
- **撤销**：按剩余有效关系重算连通组，孤片恢复独立状态。
- **有序试拼**：基于当前装配版本快照编排**非空有序**的操作序列（每步为
  「撤销一条现存有效关系」或「以两片陶片 + 成对边缘标记建立关系」），
  在内存快照上逐步推演预演，返回每步结果、最终位姿与分组（或首个失败
  原因），全程不改数据；确认时按同一版本与同一序列整体提交，任一步失败
  或版本过期均不写入任何变化，成功仅递增一次装配版本。
- **轮廓修订**：修复师发现某片陶片轮廓测量有误时，可选中该片提交
  修订后的毫米制闭合轮廓、**该片每一条有效拼接关系**在本片一侧的全新
  边缘标记及期望装配版本，先预览再确认。修订轮廓须通过现有非自交校验；
  新标记须位于修订轮廓边缘、数量与关系另一端一致、至少 2 处且同片不
  重合；随后**保持现有位姿不变**，检查受影响连通组全部有效关系的逐对
  对应点误差 ≤ 1 mm、组成员轮廓内部不重叠。独立碎片只校验轮廓。预览
  返回逐条误差与冲突原因、不落库；确认时重新校验同一输入并原子更新
  轮廓、相关标记与误差——位姿、关系 ID、连通分组与已撤销历史均不变，
  装配版本只 +1。遗漏任何有效关系、任一校验失败均整次拒绝；版本过期
  返回 409。
- **锚点整体校准**：修复师在「查看装配」页选所在组内任意一片作**锚点**
  请求整体校准。锚点位姿固定，只允许平移+旋转组内其余碎片，以**全部有效
  关系的成对边缘标记共同最小化对应点误差平方和**（最小二乘）；轮廓、标记、
  关系状态、关系 id 与连通分组均不变。预览给出调整前后各片位姿、逐关系逐
  对误差、误差总量与轮廓冲突，**不落库**。仅当**误差总量严格下降**、**每对
  对应点误差均 ≤ 1 mm** 且**组内轮廓内部不重叠**时可确认，否则列出原因并
  保持现状；确认携带预览依据的版本在同一事务内重算校验、更新位姿及每条
  有效关系的最新误差，版本只 +1 并留不可改快照。版本过期返回 409，锚点不
  存在返回 404，锚点所在组不足两片（孤片）拒绝且不写入。
- **乐观并发**：采纳 / 撤销 / 删除 / 试拼确认 / 轮廓修订确认 / 整体校准确认携带期望版本号，过期版本返回 409 冲突提示。
- **离线工作站迁移（导出 / 导入 JSON 包）**：修复师更换离线工作站时，可把
  **当前装配及全部不可改版本快照**导出为带格式版本号的 JSON 包
  （`format=pottery-assembly`、`format_version=1.0`），再导入另一台空白
  工作站。导入先**预览**包内碎片、有效及已撤销关系、版本数与导入后的装配，
  **不写库**；确认须携带目标库当前装配版本，在**单一事务**内保留碎片与关系
  的原 ID、轮廓、位姿、标记、状态（active / undone_at / created_at）、时间戳
  及各快照**原版本号、来源与快照时间**。目标库仅允许存在无碎片无关系的
  **空白基线**（不得有任何用户碎片或关系），该基线由导入包整包替换；导入
  不产生新版本号、不新增快照，装配版本直接置为包内当前版本。格式版本不支持、
  轮廓无效、关系引用缺失、快照版本重复或缺号、末版与包内当前装配不一致时
  **整包拒绝并逐条指出原因**；目标版本过期返回 409，库与历史保持原状。导入
  成功后历史查看、恢复、试拼、间隙查询与继续拼接均可正常使用。
- **陶片对间隙查询**：修复师选择一个装配版本与若干陶片对，系统依据该
  版本快照的**固定轮廓与位姿**逐对计算轮廓边界的最短间隙、对应最近点
  坐标及是否发生内部重叠，按间隙从小到大、陶片编号排序返回；全程只读，
  不因计算改变位姿、关系或历史，也不产生新快照。缺少陶片、重复陶片对、
  跨版本或无效编号时整次拒绝并逐字段指出。间隙为零表示接触，负值（-1）
  仅用于标识重叠，不得当作可用间隙。
- **不可改版本快照与历史恢复**：录入/删除碎片、采纳/撤销拼接、试拼确认、
  轮廓修订确认、锚点整体校准确认每次成功都与装配数据在**同一事务**内固化
  一条版本快照（碎片轮廓与位姿、有效及已撤销关系、连通组），快照一经写入
  不可改、不可删；校准前后两个版本均可在历史中查看/恢复；启用版本历史时
  （含旧库升级）把当前状态记为**基线**快照；预览与失败操作不产生快照。
  修复师可在「查看装配」页按版本只读回看，
  选择历史版先**预览恢复后的完整装配及与当前版的碎片/关系差异**（不写
  库），确认时提交预览依据的当前版本号：版本过期（409）或目标版不存在
  （404）均拒绝且不写入；成功后原子恢复该版碎片与关系的**原 ID 及状态**
  （含 active / undone_at / created_at、轮廓、标记、位姿），重算连通组，
  装配版本只 +1 并留下新的 `restore` 快照，旧快照全部保留仍可查。

## 一键启动（Docker Compose）

```bash
docker compose up --build
```

构建镜像时自动安装依赖，容器启动时自动初始化 SQLite 表结构与演示数据。

- **访问地址**：http://localhost:8000
- **端口**：`8000`（宿主机映射，可在 `docker-compose.yml` 的 `ports` 中修改）
- **API 文档**：http://localhost:8000/docs

## 本地开发

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

访问 http://localhost:8000 。

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_PATH` | `data/app.db`（容器内 `/app/data/app.db`） | SQLite 数据库文件路径 |
| `SEED_DEMO_DATA` | `1` | 首次启动且库为空时写入 4 片演示陶片；设为 `0` 关闭 |

Compose 默认将数据库挂载到命名卷 `pottery-data` 持久化。

## 使用流程

1. **录入碎片**：点击「录入碎片」，在主画布单击添加轮廓顶点（毫米制，
   滚轮缩放、拖拽平移），≥3 个顶点后保存；或直接粘贴坐标 JSON。
2. **新建拼接**：点击「新建拼接」，选择碎片甲 / 乙，在两个小图中点击
   轮廓边缘放置对应标记（每片 ≥2 处、数量一致，按序号一一对应）。
3. **预览**：查看组合后的各片位置、角度、每对误差与冲突原因。
4. **采纳**：校验全部通过才落库；任何冲突整次拒绝，原拼接组不变。
5. **撤销**：在「查看装配」页对有效关系点「撤销」，连通组自动重算。
6. **有序试拼**（多步预演 + 一次确认）：
   - 在「新建拼接」页放好两片标记后点「**加入试拼**」，或在「查看装配」页
     点有效关系旁的「**试拼撤销**」，把操作排入序列；
   - 侧栏「有序试拼」自动向服务器预演，逐步显示可执行 / 冲突原因，主画布
     以蓝色展示预演后的最终位姿与连通组（此时数据未改变）；
   - 全部步骤通过后点「**确认整组提交**」：按当前版本与整个序列原子写入，
     版本只 +1；任一步失败或版本过期则没有任何变化，按提示刷新后重试。
7. **修订轮廓**（测量有误时，先预览再确认）：
   - 在「查看装配」页点碎片卡片上的「**修订轮廓**」，进入修订页；
   - 在主画布单击绘制修订后的闭合轮廓（局部坐标、毫米制，可粘贴 JSON /
     一键重置为现轮廓）；
   - 每条有效拼接关系都有一个小图，在本片修订轮廓边缘点击放置新标记
     （自动吸附边缘，数量须与另一端一致、≥2 处、同片不重合；独立碎片
     无需标记）；
   - 点「**预览**」：以现有位姿在主画布显示受影响组的逐对对应点（绿色
     通过 / 红色冲突），侧栏列出逐关系逐条误差、最大误差、碰撞与冲突
     原因，此时**不写库**；
   - 预览通过后点「**确认修订**」：服务器用同一输入重新校验，原子更新
     轮廓、相关标记与误差；位姿、关系 ID、连通分组、已撤销历史均不变，
     装配版本只 +1；任何冲突整次拒绝且没有任何变化，版本过期按提示刷新。
8. **按版本查看与恢复**（历史只读 → 恢复预览 → 确认）：
   - 在「查看装配」页底部「**历史版本**」列表（新版本在前，标注变更来源
     与当前版）点「**查看**」，主画布以紫色只读展示该版的碎片轮廓、位姿、
     连通组与有效/已撤销关系，当前数据不变；
   - 点「**恢复预览**」（或历史版页的「恢复到此版本…」），主画布以蓝色
     展示恢复后的完整装配，侧栏列出与当前版的差异：碎片重现/删除/变更
     （轮廓、位姿等字段）、关系恢复/删除/变更（含「由撤销→有效」），
     以及恢复后连通组；预览**不写库**；
   - 点「**确认恢复**」：提交预览依据的当前版本号，成功则原子恢复该版
     碎片与关系的原 ID、原状态，重算连通组，装配版本只 +1，留下来源为
     「版本恢复」的新快照，旧快照仍可在列表中查看；版本过期或目标版已
     不存在则按提示刷新，不写入任何变化。
9. **整体校准**（选锚点，先预览再确认）：
   - 在「查看装配」页对组内碎片卡片点「**整体校准**」，把该片设为锚点
     （独立碎片无法校准）；
   - 点「**请求预览**」：服务端固定锚点位姿，以组内全部有效关系的成对边缘
     标记共同最小二乘，仅平移+旋转其余碎片；主画布以蓝色展示校准后位姿
     （绿色对应点通过 / 红色超限），侧栏列出调整前后各片 (x, y, 角度)、
     逐关系逐对误差（校准前 → 校准后）、误差总量、最大误差与轮廓内部
     重叠，此时**不写库**；
   - 仅当误差总量**严格下降**、每对对应点误差均 ≤ 1 mm 且组内轮廓内部不
     重叠时「**确认校准**」才可点：确认按预览依据的版本在同一事务内重算
     校验，原子更新其余碎片位姿与每条有效关系的最新误差；锚点位姿、轮廓、
     标记、关系状态、关系 id 与连通分组均不变，装配版本只 +1 并留
     「锚点整体校准」快照；
   - 不满足任一条件（误差未严格下降 / 仍有超限 / 产生轮廓重叠）则说明原因、
     保持现状；预览依据版本过期时按钮禁用，重新预览后再确认；校准前后两个
     版本都会出现在底部「历史版本」列表，可查看或恢复。
10. **离线工作站迁移**（导出包 → 空白工作站预览 → 确认导入）：
    - 在「查看装配」页底部「**离线工作站迁移**」点「**导出当前装配包
      （JSON）**」，下载带格式版本号的迁移包（含当前装配与全部不可改
      快照）；此操作只读，不改任何数据；
    - 在另一台**空白**工作站（仅启动生成过空白基线、从未录入碎片/关系）
      同一区块选择该 `.json` 文件，点「**导入预览（不写库）**」：侧栏与主
      画布（蓝色覆盖装配）展示包内碎片、有效及已撤销关系、快照版本清单、
      导入后连通组与完整装配；若目标库不是空白工作站，预览明确给出
      `ok=false` 与现状（碎片/关系/快照数），无法确认；
    - 核对无误后点「**确认导入（整包替换空白基线）**」：浏览器自动携带
      目标库当前装配版本；成功后碎片与关系的**原 ID、轮廓、位姿、标记、
      状态与全部时间戳**以及各快照的**原版本号 / 来源 / 快照时间**全部保留，
      装配版本即包内版本（不新增版本、不新增快照）；随后历史查看、恢复、
      试拼与间隙查询照常使用，新录入的碎片 / 关系 id 接续包内最大 id；
    - 格式版本不支持、轮廓无效、关系引用缺失、快照版本重复 / 缺号、末版与
      包内当前装配不一致 → 整包拒绝并逐条列出原因；目标版本过期（409）或
      目标库非空白（`target_not_blank`）均不写入，库与历史保持原状。

## API 概览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/fragments` | 录入碎片（自交轮廓 400 拒绝） |
| `GET` | `/api/fragments` | 碎片列表 |
| `DELETE` | `/api/fragments/{id}?expected_version=N` | 删除碎片及其关系 |
| `POST` | `/api/relations/preview` | 预览拼接（不落库） |
| `POST` | `/api/relations` | 采纳拼接（携带 `expected_version`） |
| `POST` | `/api/relations/{id}/undo` | 撤销关系（携带 `expected_version`） |
| `GET` | `/api/relations` | 关系列表（含已撤销历史） |
| `GET` | `/api/assembly` | 装配快照：版本、位姿、连通组、关系 |
| `GET` | `/api/assembly/versions` | 历史版本清单（版本/来源/快照时间，新版本在前） |
| `GET` | `/api/assembly/versions/{v}` | 只读查看历史版完整装配（轮廓/位姿/有效及已撤销关系/连通组） |
| `POST` | `/api/assembly/versions/{v}/gaps` | 陶片对间隙查询：按该版快照计算每对边界最短间隙/最近点/是否重叠（只读） |
| `POST` | `/api/assembly/versions/{v}/restore/preview` | 恢复预览：恢复后完整装配 + 与当前版碎片/关系差异（不落库） |
| `POST` | `/api/assembly/versions/{v}/restore/confirm` | 确认恢复（原子恢复原 ID 与状态、重算连通组，版本只 +1，留新快照） |
| `POST` | `/api/trials/rehearse` | 有序试拼预演（不落库） |
| `POST` | `/api/trials/commit` | 有序试拼整组确认（原子，版本只 +1） |
| `POST` | `/api/fragments/{id}/revision/preview` | 轮廓修订预览（逐条误差/冲突，不落库） |
| `POST` | `/api/fragments/{id}/revision/confirm` | 轮廓修订确认（重新校验同一输入，原子更新，版本只 +1） |
| `POST` | `/api/calibration/preview` | 锚点整体校准预览（调整前后位姿/逐关系误差/轮廓冲突，不落库） |
| `POST` | `/api/calibration/confirm` | 锚点整体校准确认（重算校验，原子更新位姿与关系误差，版本只 +1） |
| `GET` | `/api/transfer/export` | 导出当前装配与全部不可改快照为带格式版本号的 JSON 包（只读；`?download=true` 附件下载） |
| `POST` | `/api/transfer/import/preview` | 导入预览：包内碎片、有效/已撤销关系、版本数、导入后装配（不落库） |
| `POST` | `/api/transfer/import/confirm` | 确认导入（单事务整包替换空白基线，保留原 ID/轮廓/位姿/标记/状态/时间戳/快照原版本号） |

错误约定：`400` 校验拒绝（轮廓自交 / 标记重合 / 标记不在边缘 / 标记数量
不一致 / 遗漏有效关系 / 误差超限 / 碰撞 / 空序列 / 撤销目标非法 / 目标
版本即当前版 / **锚点所在组不足两片** / **空陶片对列表 / 无效编号 /
同片成对 / 重复陶片对 / 跨版本**），`404` 对象不存在（碎片、关系、
**目标版本快照**、**锚点碎片**缺失；版本恢复时
`detail.code=version_not_found`，校准时 `detail.code=fragment_not_found`，
**间隙查询时 `detail.code=fragment_not_found` 并逐字段指出缺失陶片**），
`409` 版本冲突或重复撤销（含恢复预览/确认、**校准预览/确认**、**导入确认**的期望版本
过期），`422` 请求体结构不合法。迁移包本身的结构 / 轮廓 / 引用 / 快照版本
/ 末版一致性问题为 `400`（`detail.code` 见「离线工作站迁移 API」的整包拒绝
表，全部原因列于 `detail.reasons[]`）；目标库非空白为
`400 target_not_blank`，预览端点对非空白目标返回 `200` 且 `ok=false`。

## 有序试拼 API

两个端点请求体相同：`expected_version`（所基于的装配版本）+ 非空 `steps`。
每个 step 通过 `action` 区分：

```jsonc
// 建立关系：两片陶片 + 成对边缘标记（每片 ≥2 处、数量一致、同片不重合）
{ "action": "relate", "fragment_a": 1, "fragment_b": 2,
  "markers_a": [[35,0],[38,15]], "markers_b": [[1,0],[4,15]],
  "client_id": "s1" }          // 可选，序列内唯一，供后续撤销步引用

// 撤销：relation_id 撤销已落库关系；client_id 撤销序列内前序新建关系
{ "action": "undo", "relation_id": 3 }
{ "action": "undo", "client_id": "s1" }
```

### 预演（不落库）

```bash
curl -X POST http://localhost:8000/api/trials/rehearse \
  -H 'Content-Type: application/json' \
  -d '{
    "expected_version": 1,
    "steps": [
      {"action":"relate","fragment_a":1,"fragment_b":2,
       "markers_a":[[35,0],[38,15],[33,30]],
       "markers_b":[[1,0],[4,15],[-1,30]],"client_id":"s1"},
      {"action":"undo","client_id":"s1"},
      {"action":"relate","fragment_a":1,"fragment_b":2,
       "markers_a":[[35,0],[38,15]],
       "markers_b":[[1,0],[4,15]],"client_id":"s2"}
    ]
  }'
```

成功响应：`ok=true`，`steps[]` 含每步类型、对应点误差、涉及碎片位姿、
当步后的连通组；`assembly` 为最终位姿 / 分组 / 关系快照（预演新建关系使用
负的临时 id，`assembly.version` 仍为所基于的版本）。

任一步失败时 HTTP 仍为 `200`，但 `ok=false`：`failed_at` 与 `failure`
给出首个失败步、错误码（如 `relation_inactive` / `fragment_missing` /
`relation_not_found` / `client_relation_not_found` / `rejected`）、人类可读
原因与冲突明细；`steps[]` 只含失败步之前的成功步，`assembly` 为失败前一刻
快照。版本过期返回 `409`。

### 确认（整体提交）

```bash
curl -X POST http://localhost:8000/api/trials/commit \
  -H 'Content-Type: application/json' \
  -d '{"expected_version":1,"steps":[
    {"action":"relate","fragment_a":1,"fragment_b":2,
     "markers_a":[[35,0],[38,15]],"markers_b":[[1,0],[4,15]],
     "client_id":"s1"}
  ]}'
# 201: {"ok":true,"version":2,"committed_steps":1,
#       "relation_ids":{"s1":1},"assembly":{...}}
```

- 全部步骤通过 → `201`，返回新版本号、`client_id → 真实关系 id` 映射和
  最新装配快照，整个序列只递增一次版本；
- 任一步失败 → `400/404/409`，`detail` 含 `failed_at / step / action /
  code / message`，**不写入任何变化**；
- `expected_version` 过期 → `409`，同样不写入。

明确的报错约定：重复撤销（关系已失效）→ `409 relation_inactive`；碎片缺失
→ `404 fragment_missing`；关系 id 缺失 → `404 relation_not_found`；撤销
未知 `client_id` → `404 client_relation_not_found`；同片建立关系、标记数量
不足 / 重合、误差 >1mm、轮廓内部重叠、闭环不符位姿 → `400 rejected`
（冲突明细在 `conflicts[]`）；空序列 / 撤销同时给或不给目标 /
`client_id` 重复 → `400`。

## 轮廓修订 API

选中碎片 `#1`，提交**修订后的毫米制闭合轮廓** `contour`、**该片每一条有效
关系**在本片一侧的新边缘标记 `relations[]`，以及所基于的
`expected_version`。每个条目按 `relation_id` 指定关系、`markers` 给出本片
（关系的 A 侧或 B 侧由系统判定）修订后的标记；关系另一端的标记不可在此
修改。

新标记的约束与建拼一致并额外要求**位于修订轮廓边缘**：数量与另一端一致、
每侧 ≥2 处、同片不重合；遗漏任何一条有效关系整次拒绝。独立碎片
（无有效关系）提交空 `relations`，只做轮廓非自交校验。

### 预览（不落库）

```bash
curl -X POST http://localhost:8000/api/fragments/1/revision/preview \
  -H 'Content-Type: application/json' \
  -d '{
    "expected_version": 3,
    "contour": [[0,0],[35,0],[38,15],[33,30],[37,45],[34,60],[0,60]],
    "relations": [
      {"relation_id": 1, "markers": [[35,0],[38,15],[33,30]]}
    ]
  }'
```

HTTP 恒为 `200`（版本过期为 `409`、碎片/关系不存在为 `404`），响应：

```jsonc
{
  "ok": true,
  "fragment_id": 1,
  "expected_version": 3,
  "independent": false,
  "missing_relation_ids": [],            // 未提交的有效关系 id（有则必拒绝）
  "contour": [[0.0,0.0], "..."],         // 规范化后的修订轮廓
  "conflicts": [],                       // 全局冲突原因（轮廓/标记/误差/碰撞）
  "involved_fragment_ids": [1, 2],       // 受影响连通组
  "relations": [
    { "relation_id": 1, "revised": true, "side": "a",
      "fragment_a": 1, "fragment_b": 2, "other_fragment_id": 2,
      "required_count": 3, "marker_count": 3,
      "errors": [0.0, 0.0, 0.0],         // 逐对对应点误差（mm，位姿保持不变）
      "max_error_mm": 0.0,
      "conflicts": [],                   // 该关系自身的冲突（超限/标记问题）
      "markers_a": [[35,0],"..."],       // 预览生效后的两侧标记（仅替换本片侧）
      "markers_b": [[1,0],"..."] }
  ],
  "overlaps": []                         // 组成员轮廓内部重叠明细（mm²）
}
```

- 受影响连通组内**不直接涉及**修订片的关系也会在 `relations[]` 中逐条核验
  （`revised=false`），其误差必须仍 ≤ 1 mm；
- `ok=false` 时 `conflicts[]` 汇总全部冲突，`relations[].conflicts` 给出逐
  关系明细，`overlaps[]` 给出碰撞双方与重叠面积；预览不写库，可修改后重预。

### 确认（原子更新，版本只 +1）

请求体与预览**完全相同**（服务端重新校验同一输入）：

```bash
curl -X POST http://localhost:8000/api/fragments/1/revision/confirm \
  -H 'Content-Type: application/json' \
  -d '{
    "expected_version": 3,
    "contour": [[0,0],[35,0],[38,15],[33,30],[37,45],[34,60],[0,60]],
    "relations": [
      {"relation_id": 1, "markers": [[35,0],[38,15],[33,30]]}
    ]
  }'
# 201: {"ok":true,"fragment_id":1,"version":4,"assembly":{...}}
```

- 全部通过 → `201`：原子写入修订轮廓、每条相关关系本片侧的新标记及最新
  最大误差；**位姿（x/y/theta）、关系 id、连通分组（group_id）、已撤销
  历史（active/undone_at/created_at）保持不变**，装配版本只 +1，返回最新
  装配快照；
- 任一校验失败 → `400`，`detail` 内附与预览相同结构的逐条结果
  （`conflicts / relations / overlaps / missing_relation_ids`），**不写入
  任何变化**；
- `expected_version` 过期 → `409` 冲突提示，不写入；碎片不存在 → `404`；
  提交了库里不存在的关系 id → `404 relation_not_found`；提交已撤销 / 不涉及
  本片的关系、同关系重复提交、遗漏有效关系、标记数量不一致 / 不足 2 处 /
  同片重合 / 不在修订轮廓边缘 → `400`。

## 锚点整体校准 API

修复师从当前装配选一片碎片作**锚点**，对其所在连通组请求整体校准：

- 锚点的位姿（x/y/theta）固定不变，只允许调整组内**其余**碎片的平移与
  旋转；轮廓、边缘标记、关系状态（active/undone_at/created_at）、关系 id
  与连通分组均不变；
- 以组内**全部有效关系**的成对边缘标记构造对应点，最小二乘共同最小化
  全部对应点世界坐标距离的误差平方和（高斯-牛顿/LM）；
- 预览不落库；确认必须满足三个条件，否则整次拒绝、保持现状：
  1. 误差总量（所有对应点误差平方和，mm²）**严格下降**；
  2. **每对**对应点校准后误差均 ≤ 1 mm；
  3. 组内成员轮廓（校准后位姿）**内部不重叠**（允许共边/共点）。

请求体两端点相同：`anchor_id`（锚点碎片）+ `expected_version`（预览所
依据的当前装配版本）。

### 预览（不落库）

```bash
curl -X POST http://localhost:8000/api/calibration/preview \
  -H 'Content-Type: application/json' \
  -d '{"anchor_id": 1, "expected_version": 3}'
```

HTTP 恒为 `200`（版本过期为 `409`、锚点不存在为 `404 fragment_not_found`、
锚点为孤片为 `400 group_too_small`），响应：

```jsonc
{
  "ok": true,                        // 三个条件是否全部满足（是否可确认）
  "anchor_id": 1,
  "expected_version": 3,
  "involved_fragment_ids": [1, 2, 3],
  "sse_before_mm2": 17.19,           // 校准前误差总量（全部对应点误差平方和）
  "sse_after_mm2": 0.0,              // 校准后误差总量
  "sse_strictly_decreased": true,    // 条件 1：误差总量严格下降
  "max_error_before_mm": 1.31,
  "max_error_after_mm": 0.0,
  "all_pair_errors_within_1mm": true,// 条件 2：每对对应点误差均 ≤ 1 mm
  "conflicts": [],                   // 不满足条件的全部原因（可多条）
  "poses": [                         // 调整前后各片位姿（锚点 anchor=true 且不变）
    { "fragment_id": 1, "name": "陶片A-左半", "anchor": true, "moved": false,
      "before": {"x": 0.0, "y": 0.0, "theta_deg": 0.0},
      "after":  {"x": 0.0, "y": 0.0, "theta_deg": 0.0} },
    { "fragment_id": 2, "name": "陶片B-右半", "anchor": false, "moved": true,
      "before": {"x": 40.6, "y": 0.4, "theta_deg": 1.72},
      "after":  {"x": 40.0, "y": 0.0, "theta_deg": 0.0} }
  ],
  "relations": [                     // 逐关系逐对对应点误差（校准前 → 校准后）
    { "relation_id": 1, "fragment_a": 1, "fragment_b": 2,
      "name_a": "陶片A-左半", "name_b": "陶片B-右半", "marker_pairs": 3,
      "errors_before": [0.72, 0.39, 0.71],
      "errors_after":  [0.0, 0.0, 0.0],
      "max_error_before_mm": 0.72, "max_error_after_mm": 0.0 }
  ],
  "overlaps": []                     // 条件 3：校准后轮廓内部重叠明细（mm²）
}
```

- `ok=false` 时 `conflicts[]` 会同时列出「误差总量未严格下降 / 某对误差
  超限（给出关系与序号）/ 哪两片轮廓重叠及面积」全部原因；预览不写库，
  可换锚点后重新预览。

### 确认（原子更新，版本只 +1）

请求体与预览**完全相同**（服务端以同一锚点重新演算并重新校验）：

```bash
curl -X POST http://localhost:8000/api/calibration/confirm \
  -H 'Content-Type: application/json' \
  -d '{"anchor_id": 1, "expected_version": 3}'
# 201: {"ok":true,"anchor_id":1,"version":4,"assembly":{...}}
```

- 全部条件满足 → `201`：在锁与同一事务内更新其余碎片位姿 (x/y/theta) 与
  每条有效关系的最新最大误差；**锚点位姿、轮廓、标记、关系状态、关系 id
  与连通分组保持不变**；装配版本只 +1，返回最新装配快照，并固化一条
  `source=calibration` 的不可改快照（校准前版本快照仍保留、可查看/恢复）；
- 任一条件不满足 → `400`，`detail` 内附与预览相同结构的结果
  （`conflicts / poses / relations / overlaps / sse_*`），**不写入任何变化**；
- `expected_version` 过期 → `409`，不写入；锚点不存在 → `404
  fragment_not_found`；锚点所在组不足两片 → `400 group_too_small`，均不写入。

## 历史版本 API

碎片录入 / 删除、拼接采纳 / 撤销、试拼确认、轮廓修订确认、锚点整体校准
确认每次成功都在同一事务内写入不可改快照（校准前后两个版本均可查看/恢复）；
服务启用时（含旧库升级、首次空库初始化）若尚无快照，
把当前状态记为 `baseline` 基线。快照来源 `source` 取值：

| source | 含义 |
| --- | --- |
| `baseline` | 基线（启用版本历史时记录，旧库升级为当前版补记） |
| `create` / `delete` | 录入 / 删除碎片 |
| `accept` / `undo` | 采纳 / 撤销拼接 |
| `trial` | 有序试拼整组确认 |
| `revision` | 轮廓修订确认 |
| `calibration` | 锚点整体校准确认 |
| `restore` | 历史版本恢复（恢复后留下的新快照） |

预览（拼接 / 试拼预演 / 修订预览 / **校准预览** / 恢复预览）与任何失败、
过期操作都**不产生快照**。

### 版本清单

```bash
curl http://localhost:8000/api/assembly/versions
```

```jsonc
{
  "current_version": 3,
  "versions": [
    { "version": 3, "source": "undo", "source_label": "撤销拼接",
      "created_at": "2026-10-05T08:01:38+00:00", "current": false },
    { "version": 2, "source": "accept", "source_label": "采纳拼接", "...": "…" },
    { "version": 1, "source": "baseline", "source_label": "基线（启用版本历史时记录）",
      "created_at": "…", "current": false }
  ]
}
```

### 查看历史版（只读）

```bash
curl http://localhost:8000/api/assembly/versions/2
```

响应与 `GET /api/assembly` 同结构，并附版本元信息；`assembly.historical=true`，
`assembly.version` 为被查看的版本。可查看该版**碎片轮廓、位姿（x/y/theta）、
连通组、全部关系（含 active=false 的已撤销关系及其 undone_at）**：

```jsonc
{
  "version": 2, "source": "accept", "source_label": "采纳拼接", "created_at": "…",
  "assembly": {
    "version": 2, "historical": true,
    "fragments": [ { "id": 1, "name": "陶片A-左半", "contour": [[0,0], "..."],
                     "x": 0, "y": 0, "theta": 0, "group_id": 1, "created_at": "…" } ],
    "groups": [ { "id": 1, "fragment_ids": [1, 2] } ],
    "relations": [ { "id": 1, "fragment_a": 1, "fragment_b": 2,
                     "markers_a": ["..."], "markers_b": ["..."], "max_error": 0.0,
                     "active": true, "created_at": "…", "undone_at": null } ]
  }
}
```

目标版本快照不存在 → `404 {"detail":{"code":"version_not_found", …}}`。

### 恢复预览（不落库）

请求体只含预览所依据的**当前**装配版本：

```bash
curl -X POST http://localhost:8000/api/assembly/versions/2/restore/preview \
  -H 'Content-Type: application/json' -d '{"expected_version":3}'
```

响应给出恢复后的完整装配（`restored_assembly`，按目标版碎片重算连通组）与
逐对象差异 `diff`；`new_version` 为确认成功后的版本（当前 +1）。预览不改
当前数据、不产生快照：

```jsonc
{
  "ok": true, "expected_version": 3, "target_version": 2,
  "source": "accept", "source_label": "采纳拼接", "snapshot_created_at": "…",
  "new_version": 4,
  "restored_assembly": { "version": 2, "historical": true,
    "fragments": ["..."], "groups": ["..."], "relations": ["..."] },
  "diff": {
    "current_version": 3, "target_version": 2,
    "fragments": {
      "restored": [ { "id": 5, "target": { "…": "目标版碎片（恢复后重现）" } } ],
      "removed":  [ { "id": 6, "current": { "…": "当前版碎片（恢复后删除）" } } ],
      "changed":  [ { "id": 2, "changed_fields": ["x","theta"],
                      "current": {"…": "…"}, "target": {"…": "…"} } ],
      "unchanged": [ { "id": 1 } ]
    },
    "relations": {
      "restored": [ { "id": 1, "target": { "active": true, "…": "曾被删除的关系随恢复重现" } } ],
      "removed":  [ { "id": 7, "current": { "…": "目标版之后建立、恢复后删除" } } ],
      "changed":  [ { "id": 1, "changed_fields": ["active","undone_at"],
                      "current": { "active": false, "undone_at": "…" },
                      "target":  { "active": true,  "undone_at": null } } ],
      "unchanged": [ { "id": 3, "active": true } ]
    },
    "groups": { "current":  ["..."], "target": ["..."] },
    "summary": { "fragments": { "restored": 1, "removed": 1, "changed": 1, "unchanged": 2 },
                 "relations": { "restored": 0, "removed": 1, "changed": 1, "unchanged": 0 } }
  }
}
```

- 期望版本过期 → `409`（同其它端点的版本冲突约定）；
- 目标版不存在 → `404 version_not_found`；目标版即当前版 →
  `400 target_is_current`（无需恢复）。

### 确认恢复（原子，版本只 +1）

请求体与预览**完全相同**（只提交 `expected_version`，即预览依据的当前版）：

```bash
curl -X POST http://localhost:8000/api/assembly/versions/2/restore/confirm \
  -H 'Content-Type: application/json' -d '{"expected_version":3}'
# 201:
# {"ok":true,"restored_from_version":2,"expected_version":3,"version":4,
#  "assembly":{ "version":4, "fragments":[…], "groups":[…], "relations":[…] }}
```

- 成功：在锁与同一事务内**原子恢复目标版碎片与关系的原 ID 及状态**
  （轮廓、位姿、标记、`max_error`、`active`、`created_at`、`undone_at`
  全部按该版原样），目标版之后新增的碎片/关系删除、删除的按原 ID 重现，
  随后按有效关系**重算连通组**；装配版本只 +1，并留下 `source=restore`
  的新快照；**旧快照（含被恢复版本身）全部保留、仍可查看/再次恢复**；
- `expected_version` 与当前版不符（预览后数据已被他人变更）→ `409`，
  不写入；目标版不存在 → `404 version_not_found`，不写入；目标版即当前版
  → `400`，不写入。

## 离线工作站迁移 API

修复师更换离线工作站时，把**当前装配及全部不可改版本快照**导出为带格式
版本号的 JSON 包，再导入另一台空白工作站。三个端点均为顶层 JSON：

| 端点 | 语义 |
| --- | --- |
| `GET /api/transfer/export` | 导出迁移包（只读，不产生新快照；`?download=true` 时 `Content-Disposition: attachment` 直接下载） |
| `POST /api/transfer/import/preview` | 整包校验 + 导入预览（**不写库**）；目标非空白时 HTTP 200、`ok=false` |
| `POST /api/transfer/import/confirm` | 确认导入（携带 `expected_version`，单事务整包替换空白基线） |

### 迁移包格式（`format_version` = `1.0`）

```jsonc
{
  "format": "pottery-assembly",       // 固定格式标识
  "format_version": "1.0",            // 格式版本号（不支持的版本整包拒绝）
  "exported_at": "2026-10-06T03:10:00+00:00",
  "current_version": 2,               // 包内当前装配版本
  "current_assembly": {               // 当前完整装配（末版快照与之逐对象一致）
    "version": 2,
    "fragments": [ { "id": 1, "name": "陶片A-左半", "contour": [[0,0], "..."],
                     "x": 0.0, "y": 0.0, "theta": 0.0, "group_id": 1,
                     "created_at": "…" } ],
    "groups": [ { "id": 1, "fragment_ids": [1, 2] } ],
    "relations": [ { "id": 1, "fragment_a": 1, "fragment_b": 2,
                     "markers_a": ["..."], "markers_b": ["..."], "max_error": 0.0,
                     "active": true, "created_at": "…", "undone_at": null } ]
  },
  "snapshots": [                      // 全部不可改快照，按版本升序，原版本号/时间
    { "version": 1, "source": "baseline", "created_at": "…", "state": { "…": "…" } },
    { "version": 2, "source": "accept",   "created_at": "…", "state": { "…": "…" } }
  ]
}
```

### 导出

```bash
# 直接获取 JSON（可重定向保存）
curl http://localhost:8000/api/transfer/export -o pottery-assembly-v2.json

# 或要求附件下载（响应头带建议文件名）
curl -OJ http://localhost:8000/api/transfer/export?download=true
```

导出只读：不改位姿、关系或历史，不产生新快照。

### 导入预览（不写库）

```bash
curl -X POST http://localhost:8000/api/transfer/import/preview \
  -H 'Content-Type: application/json' \
  --data-binary @- <<'JSON'
{ "package": { "format": "pottery-assembly", "format_version": "1.0", "…": "…" } }
JSON
```

> `package` 字段即导出文件的完整 JSON 内容（文件上传 / 粘贴均可，包本身
> 不嵌在 `{}` 之外）。成功响应：

```jsonc
{
  "ok": true,                        // 目标库是否为可导入的空白工作站
  "package": {
    "format": "pottery-assembly", "format_version": "1.0",
    "exported_at": "…", "current_version": 2, "snapshot_count": 2,
    "versions": [ { "version": 1, "source": "baseline", "created_at": "…" },
                  { "version": 2, "source": "accept",   "created_at": "…" } ]
  },
  "target": { "current_version": 0, "fragment_count": 0, "relation_count": 0,
              "snapshot_count": 1, "nonempty_snapshot_versions": [] },
  "fragment_count": 4,
  "active_relation_count": 1,
  "undone_relation_count": 0,
  "active_relations": [ "…" ],       // 包内有效关系
  "undone_relations": [],            // 包内已撤销关系（含 undone_at）
  "fragments": [ "…" ],
  "imported_assembly": { "version": 2, "fragments": ["…"],
                         "groups": [{ "id": 1, "fragment_ids": [1, 2] }],
                         "relations": ["…"] },
  "imported_version": 2,
  "reasons": []
}
```

- 目标库非空白（存在用户碎片 / 关系，或曾有非空快照）→ HTTP 仍为 `200`，
  但 `ok=false`、`reasons[]` 说明原因、`target` 给出现状；
- 包本身有问题 → `400`，`detail.code` 为主原因、`detail.reasons[]` 一次性
  列出**全部**拒绝原因。

### 确认导入（单事务原子写入）

```bash
curl -X POST http://localhost:8000/api/transfer/import/confirm \
  -H 'Content-Type: application/json' \
  -d '{"expected_version": 0, "package": { "format": "pottery-assembly",
        "format_version": "1.0", "…": "…" }}'
# 201:
# {"ok":true,"imported_version":2,"snapshot_count":2,"expected_version":0,
#  "assembly":{ "version":2,"fragments":[…],"groups":[…],"relations":[…] }}
```

- 成功：在锁与**同一事务**内以包替换空白基线——碎片 / 关系保留**原 ID**，
  轮廓、位姿 (x/y/theta)、两侧标记、`max_error`、`active`、`created_at`、
  `undone_at` 全部原样；快照以**原版本号**、原 `source`、原 `created_at`、
  原 `state` 写回；`meta.version` 置为包内 `current_version`（**不新增版本、
  不新增快照**）；随后按有效关系重算连通组，AUTOINCREMENT 计数回退到包内
  最大 id（新碎片 / 关系接续编号，不跳号）；
- 导入后历史查看 (`GET …/versions/{v}`)、恢复预览 / 确认、试拼、间隙查询
  与继续采纳 / 撤销 / 修订 / 校准均基于原版本号正常工作；
- `expected_version` 与目标库当前版本不符 → `409`，库与历史保持原状；
- 目标库非空白 → `400 target_not_blank`（`detail.target` 给出现状），
  不写入。

### 整包拒绝原因（`detail.code`，400，全部原因在 `reasons[]` 列出）

| code | 触发条件 |
| --- | --- |
| `unsupported_format_version` | `format_version` 不受支持（仅接受 `1.0`） |
| `invalid_format` | 顶层 `format` 标识不是 `pottery-assembly` |
| `invalid_contour` | 任一碎片 / 快照轮廓不是有效的毫米制非自交闭合轮廓 |
| `relation_reference_missing` | 当前装配或任意外部快照中的关系引用了包内不存在的碎片 |
| `duplicate_snapshot_version` | 快照版本号重复 |
| `snapshot_version_gap` | 快照版本不连续（必须自 v0/v1 起无缺号） |
| `current_assembly_mismatch` | 末版快照与包内 `current_assembly` 逐对象不一致 |
| `invalid_current_version` / `invalid_snapshot_version` | 版本号不是非负整数、state.version 与快照版本不符等 |
| `invalid_fragment` / `invalid_relation` / `invalid_markers` / `invalid_timestamp` / … | 字段缺失、类型错误、标记不成对、空名称等结构问题 |
| `target_not_blank` | 仅确认端点：目标库存在用户碎片 / 关系或非空历史（空白基线除外） |

任何拒绝（含 400 / 409）都在事务提交前回滚，**库与历史保持原状**。

### 跨工作站完整调用示例

```bash
# 旧工作站：导出（库 data/app.db，当前 v2，含 v1/v2 两个快照）
curl http://old-host:8000/api/transfer/export -o pottery-v2.json

# 新工作站（空白库，启动后只有 v0 空白基线）
curl -s http://new-host:8000/api/assembly | jq '.version'      # => 0

# 1) 先预览（jq 包裹文件内容为 {"package": …}）
jq -Rs '{package: fromjson? // .}' pottery-v2.json \
  | curl -s -X POST http://new-host:8000/api/transfer/import/preview \
      -H 'Content-Type: application/json' --data-binary @- \
  | jq '{ok, fragment_count, active_relation_count, undone_relation_count,
         imported_version, groups: .imported_assembly.groups, reasons}'

# 2) 确认（携带预览时读到的目标库版本 0）
jq -Rs '{expected_version: 0, package: fromjson}' pottery-v2.json \
  | curl -s -X POST http://new-host:8000/api/transfer/import/confirm \
      -H 'Content-Type: application/json' --data-binary @- \
  | jq '{ok, imported_version, snapshot_count}'
# => {"ok": true, "imported_version": 2, "snapshot_count": 2}

# 历史与拼接照常：查看 / 恢复 v1、继续在 v2 上操作
curl http://new-host:8000/api/assembly/versions | jq '.versions[].version'  # 2, 1
```

## 陶片对间隙查询 API

修复师选择**一个装配版本**与**若干陶片对**，系统依据该版本快照中固定的
轮廓与位姿（世界坐标，毫米制）逐对计算：

- 两片轮廓**边界**的最短间隙 `gap_mm`；
- 对应的最近点坐标 `point_a` / `point_b`（分别位于两片轮廓边界上）；
- 是否发生内部重叠 `overlap`（允许共边/共点，面积重叠才算重叠，附
  `overlap_area_mm2`）。

**间隙语义**：`0` 表示两边界接触（`contact=true`）；负值（固定 `-1`）
**仅用于标识内部重叠**，无物理意义，**不得当作可用间隙**。结果按间隙
从小到大、再按陶片编号（`fragment_a`、`fragment_b`）排序返回。

**只读保证**：仅读取指定版本的不可改快照，不修改位姿、关系或历史，也
不产生新版本快照；当前版与任意历史版均可查询。

### 请求

```
POST /api/assembly/versions/{version}/gaps
```

```jsonc
{
  "pairs": [
    { "fragment_a": 1, "fragment_b": 2 },
    { "fragment_a": 1, "fragment_b": 3 },
    { "fragment_a": 2, "fragment_b": 4 }
    // 每对可选 "version": 必须与路径版本一致，否则视为跨版本整次拒绝
  ]
}
```

### 调用示例

```bash
curl -X POST http://localhost:8000/api/assembly/versions/2/gaps \
  -H 'Content-Type: application/json' \
  -d '{"pairs": [{"fragment_a": 1, "fragment_b": 2},
                 {"fragment_a": 1, "fragment_b":  3},
                 {"fragment_a": 2, "fragment_b": 4}]}'
```

响应（按间隙升序；演示数据下 #1/#3 重叠、#1/#2 共边接触、#2/#4 分离）：

```jsonc
{
  "ok": true,
  "version": 2,
  "source": "accept",
  "source_label": "采纳拼接",
  "snapshot_created_at": "2026-10-06T00:41:27+00:00",
  "current_version": 2,
  "historical": false,               // 查询的是否为历史版本
  "pair_count": 3,
  "pairs": [
    { "fragment_a": 1, "fragment_b": 3,
      "name_a": "陶片A-左半", "name_b": "陶片C",
      "gap_mm": -1.0,                // 负值仅标识内部重叠，非可用间隙
      "contact": false, "overlap": true,
      "overlap_area_mm2": 868.321429,
      "point_a": [0.0, 0.0], "point_b": [0.0, 0.0] },
    { "fragment_a": 1, "fragment_b": 2,
      "name_a": "陶片A-左半", "name_b": "陶片B-右半",
      "gap_mm": 0.0,                 // 间隙为零表示接触
      "contact": true, "overlap": false, "overlap_area_mm2": 0.0,
      "point_a": [38.0, 15.0], "point_b": [38.0, 15.0] },
    { "fragment_a": 2, "fragment_b": 4,
      "name_a": "陶片B-右半", "name_b": "陶片D",
      "gap_mm": 5.8835,              // 边界最短间隙（毫米）
      "contact": false, "overlap": false, "overlap_area_mm2": 0.0,
      "point_a": [35.7692, 3.8462], "point_b": [30.0, 5.0] }
  ]
}
```

### 整次拒绝（逐字段指出，不执行任何计算）

- `400 empty_pairs`：`pairs` 为空；
- `400 invalid_pairs`：`detail.errors[]` 逐字段列出全部问题——
  `invalid_fragment_id`（编号非正整数，字段如 `pairs[0].fragment_a`）、
  `same_fragment`（两片相同）、`duplicate_pair`（重复陶片对，不分先后）、
  `cross_version`（某对自带 `version` 与路径版本不一致，仅允许查询同一
  版本快照）；
- `404 fragment_not_found`：某对引用的碎片在该版本快照中不存在，
  `detail.errors[]` 逐字段指出（如 `pairs[0].fragment_b`）；
- `404 version_not_found`：目标版本快照不存在；
- `422`：请求体结构不合法（如编号为字符串）。

```bash
# 重复陶片对 + 无效编号 + 跨版本：一次全部指出
curl -X POST http://localhost:8000/api/assembly/versions/2/gaps \
  -H 'Content-Type: application/json' \
  -d '{"pairs": [{"fragment_a": 1, "fragment_b": 2},
                 {"fragment_a": 2, "fragment_b": 1},
                 {"fragment_a": 0, "fragment_b": 3},
                 {"fragment_a": 3, "fragment_b": 4, "version": 1}]}'
# 400 {"detail":{"code":"invalid_pairs","errors":[
#   {"field":"pairs[1]","code":"duplicate_pair", ...},
#   {"field":"pairs[2].fragment_a","code":"invalid_fragment_id", ...},
#   {"field":"pairs[3].version","code":"cross_version", ...}]}}
```

## 项目结构

```
app/
  main.py          # FastAPI 路由、单条拼接、版本控制、试拼/修订/校准/历史恢复/间隙查询/离线迁移端点
  history.py       # 历史版本：来源标签、连通组重算、碎片/关系差异演算（只读）
  portable.py      # 离线工作站迁移：JSON 包导出、整包校验、导入预览演算（只读）
  revision.py      # 轮廓修订：边缘标记校验、受影响组误差/碰撞演算（只读）
  calibration.py   # 锚点整体校准：固定锚点的组内刚体最小二乘、误差/碰撞演算（只读）
  gaps.py          # 陶片对间隙查询：版本快照上的边界最短间隙/最近点/重叠演算（只读）
  trial.py         # 有序试拼：内存快照逐步推演 + 整组提交 + 位姿演算
  geometry.py      # 刚体配准（最小二乘）、轮廓/边缘标记校验、重叠检测
  database.py      # SQLite 持久化、连通组重算、不可改版本快照、按版恢复、迁移包整包导入
  static/          # 浏览器二维画布前端（原生 JS）
tests/
  test_gaps.py     # 陶片对间隙查询 API 测试
  test_transfer.py # 离线工作站迁移（导出/预览/确认/整包拒绝/空白约束/409）测试
Dockerfile
docker-compose.yml
requirements.txt
```
