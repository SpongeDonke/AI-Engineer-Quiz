# SUBMISSION

素材检索与导出的实现说明。分支与最终 commit SHA 见回复邮件（本文件随该 commit 一起提交）。

## 运行

依赖见 `pyproject.toml`，Python 3.10+。README 里的 uv 流程可以直接用：

```bash
uv venv --python 3.12
uv pip install -e '.[dev]'

export KEEPSTACK_AI_ENABLED=false
export KEEPSTACK_OCR_ENABLED=false

uv run --no-sync python -m keepstack seed
uv run --no-sync python -m keepstack demo     # 本题场景的复现数据
uv run --no-sync python -m keepstack
```

没有 uv 时用 venv + pip 等价：

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows；POSIX 用 source .venv/bin/activate
pip install -e '.[dev]'
python -m keepstack seed
python -m keepstack demo
python -m keepstack
```

打开 <http://localhost:8000>，`admin` / `admin`。`seed` 生成原有的 24 个素材和 3 个集合，
`demo` 额外建一个 `Harbor Campaign` 集合，用来复现题目里的业务场景；重复执行不会产生重复数据
（按内容寻址去重后跳过）。

`demo` 的数据是固定生成的：18 个素材，其中

| 素材 | 标签 | 说明 |
| --- | --- | --- |
| 14 个 `Harbor ...` | approved + campaign | 应命中 |
| 2 个 `Harbor ...` | approved + campaign + internal | 应被排除 |
| 1 个 `Harbor ...` | approved | 缺 campaign，不命中 |
| 1 个 `Quay office window` | campaign | 不含关键词，不命中 |

其中前两个的 `created_at` 都是 `2026-03-01T09:00:00Z`，用来观察相同创建时间下的分页稳定性。

## 手工验证步骤

1. 登录后进 Library。默认每页 12 条，能看到总数与翻页控件。
2. Collection 选 `Harbor Campaign`，结果 18 条。
3. 搜索框输入 `harbor`。
4. Include tags 输入 `approved` 回车，再输入 `campaign` 回车。标签大小写不敏感，重复添加不生效。
5. Exclude tags 输入 `internal` 回车。
6. 结果变为 14 条，两页（12 + 2），默认 newest，即先看 `Harbor customs office` 一直到
   `Harbor pilot boat`，第二页是那两条时间相同的 `Harbor crane in the mist`、`Harbor at dawn`。
7. 切 oldest，顺序整体反过来；切页码、来回翻，条目不重复不遗漏。
8. 点 Export CSV，下载 `keepstack-assets-<日期>.csv`，14 行数据 + 表头，顺序与列表一致
   （不受当前页限制）。
9. 筛选条件下的条目会以 chip 形式显示，点 chip 上的 × 只去掉该条件，改条件后回到第一页。
10. 打开任一命中素材改标题或加/删标签，关闭侧栏后列表、再导出都已是新值。

## 实测结果

- `python -m pytest -q`：**39 passed, 2 skipped**。跳过的是原有的两个用例，需要本地
  CLIP 视觉模型和 tesseract，装了 AI 依赖的环境才会跑（未做改动）。
- 新增用例覆盖：组合筛选（关键词 / 集合 / 多包含标签 / 多排除标签）、同一标签既包含又排除、
  重复标签、排序与关键词同用、跨页不重不漏、相同 `created_at` 的稳定顺序、越界 offset、
  非法 `limit`/`offset`、素材类型筛选同时作用于列表与导出、导出行与列表顺序一致、
  零结果仍输出表头、逗号 / 引号 / 换行 / 中文的 CSV 转义、登录鉴权、修改标题与标签后的查询
  与导出结果。
- 浏览器走查（本地无头 Chrome 驱动真实页面）：`admin` / `admin` 登录 → 默认 42 条 4 页；
  集合筛选 18 条 2 页；`approved` → 17、`+campaign` → 16、`-internal` → 14；chip 删除单个条件后
  回到 16 并回到第一页；Export 提示 `Exported 14 assets`；建集合、加标签、创建分享链接
  均成功并能读回；控制台无报错。
- 导出文件：UTF-8（带 BOM，Excel 直接打开中文不乱码）、CRLF 换行；`Harbor at dawn` 的描述
  含逗号、引号和换行，导出后被正确加引号，用 `csv` 读回来与库中值逐字一致。

## 实现选择

先修了一个挡路的既有缺陷：`web/app.js` 的 `api()` 对 `{ json: ... }` 只设了 Content-Type 和
body，没有设 `method`，浏览器把它当成"带 body 的 GET"直接拒绝，于是登录、加标签、新建集合、
分享链接全部失效（后端本身是好的）。改成有 json 时默认 `POST`，调用方显式传 `method` 的
（PATCH / DELETE）不受影响。

查询集中在 `keepstack/search.py`：

- 关键词仍然走原有 `asset_fts` 和 `_fts_query`，空输入不加条件；标点输入清洗后为空时同样视为
  不限制关键词，而不是让 FTS5 报语法错。
- 标签、集合用 `EXISTS` 子查询而不是 JOIN。JOIN 遇到多标签会把一行放大成多行，`COUNT(*)`
  和 `LIMIT/OFFSET` 就都对不上了；`EXISTS` 保持一行一素材，包含标签全部命中、排除标签命中任一
  即排除，同一标签出现在两边时自然返回空。
- 排序统一为 `created_at` + 主键兜底（`oldest` 用 `id ASC`，`newest` 用 `id DESC`），
  相同创建时间也不会跨页重复或遗漏；带关键词时不再被 `bm25` 覆盖，仍按所选排序。
- 列表和导出共用同一套 `_where()` 与 `SORTS`，导出因此不可能和列表筛选不一致。
- 分页用 `limit` / `offset`，返回 `total`、`page`、`pages`；`limit` 限制 1–100，
  `offset` 不能为负，越界返回 400 而不是悄悄改成别的页。
- 导出在 `GET /api/export/assets.csv`，用 `csv` 模块写，标签以 `;` 连接（单标签时就是标签名
 本身），零结果时只写表头；沿用现有 Bearer 鉴权，并记一条 `asset.export` 审计。

前端（`web/app.js`、`web/index.html`、`web/styles.css`）：

- 顶部加了 Collection / Include tags / Exclude tags 三个输入，标签输入框回车成条件，
  当前生效的条件用一行 chip 展示，点 chip 的 × 删掉单条。
- 每次请求带自增序号，晚到的旧响应直接丢弃，快速切条件时最终结果一定对应最新条件。
- 条件或排序变化回到第一页；结果变少导致当前页越界时自动退到最后一页。
- 加载失败在列表位置显示错误条，不把上一次结果擦掉；导出失败弹 `err` toast。
- 语义搜索入口在页面上隐藏（题目允许），后端 `mode=semantic` 仍在，并且现在也遵守集合 /
  标签 / 素材类型筛选，只是用向量排序替代关键词。

## 取舍与已知限制

- 隐藏语义搜索入口是因为只验收关键词；保留后端接口不为它做 UI，避免一个未验收的入口分散注意力。
- 分页用 offset。题目只要求数据不变时的一致性，没有做跨请求快照；很深的翻页（十万级之后）
  offset 会变慢，这也说明导出没走分页、一次性取全量。
- 导出同步生成，全量在内存里拼 CSV。几万行没问题；再大应该改成流式响应，这次没做。
- `sort` 传了未知值时回退 `newest`，而不是像 `limit` 那样报 400。原因是列表页的排序值来自
  前端固定下拉框，拼错不会发生；而分页参数可能被外部脚本传错，报错更有用。
- 侧栏的标签 / 类型计数仍是全库口径（标题写着 Top tags，没有标成当前匹配数），题目允许不联动。
- 顺手发现但没改：`DELETE /api/assets/{uuid}/tags/{name}` 之后没有刷新 embedding，语义模式下
  会用到旧向量。它不影响关键词、筛选和导出，属于范围外，记在这里。

## 未完成项

- 没有把浏览器走查脚本纳入仓库（只在本地跑过），自动化停在 API 层。
- 导出没有列选择、进度显示或取消。
- 没有为 CSV 列顺序做可配置项，列固定为
  `uuid, title, description, filename, media_type, tags, created_at`。
