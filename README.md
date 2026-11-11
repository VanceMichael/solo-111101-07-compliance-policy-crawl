# 产品合规规则版本化采集

离线采集工程使用 Python 标准库和 SQLite。adapter 读取本项目的真实来源格式，sources 提供可重复响应序列，capture 原子归档解析后的来源记录。已有 --capture 只记录原始观察，不作业务投影；--run 工作流是待扩展功能。

## 环境与准备

推荐 Python 3.12.12，支持 Python 3.11+；requirements.lock 为空表示没有第三方包，运行、测试和验收均不下载数据或依赖。Dockerfile 提供同一 Linux 环境。需要容器时先在正式运行之外准备：

```sh
docker build -t q007-compliance-policy-crawl:initial .
docker run --rm q007-compliance-policy-crawl:initial
```

## 基线测试与原始采集

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q collector_07 tests acceptance
scratch=$(mktemp -d)
python3 -m collector_07.cli --database "$scratch/raw.sqlite3" --capture fixtures --route /rule/v1
python3 -m collector_07.cli --database "$scratch/raw.sqlite3" --capture fixtures --route /rule/v1
```

第一次 decoded=1、inserted=1，重抓同一观测 inserted=0。临时库不提交。tests 是已实现采集基础能力的回归测试，不代表扩展功能完成。

## 来源协议

源 JSON 是网页/附件提取后的规则条目，format 指载体，规则身份为 rule+version；正文、范围、生效日相同而载体变化不产生新规则版本，但保存两个 carriers。published_at 是当时可知时间，effective_at 是业务生效时间；known_on 之前尚未发布的规则不能参与历史选取，即使追溯生效。

fixtures/context.json 固定商品范围 CHILD-1=child、ADULT-1=adult，以及 AUDIT-1 在 11月5日按版本1审核的引用。新版本追溯生效时计算通知受影响商品，但不修改既有 audits 引用。每个商品版本通知去重；304 只有已缓存同规则时为 unchanged，否则 unknown_cache；503 为 unavailable，不等同无变化，不能删除既有版本。

FixtureTransport 每次进程创建从 routes.json 首条响应开始，同一路由多次 request 依次推进，最后一条可重复；calls 保存请求证据。源数据相对 fixture_root 读取，不能越出该目录。所有响应是本地合成数据，不发出公网请求。单场景 JSON 的 fixture_root 相对当前工作目录解析；在仓库根目录执行即可。run_id 是采集批次标识，name 仅作展示标签。

## 扩展接口与可观察输出

`python3 -m collector_07.cli --run 场景文件 --database 状态库 --output 结果文件`

场景文件含 name、run_id、sources（有序根来源）、options、fixture_root；name 只作日志标签。`options.effective_on` 是审核发生日，`options.known_on` 是本次采集的可知日期，均为 `YYYY-MM-DD`。expected 是公开最小预期，产品不应读取它。所有来源和选项应独立支持；四个场景不能替代全部边界测试。业务结果投影：selected、versions、carriers、notifications、audits、observations（route 与 result）；所有历史查询由 effective_on 与 known_on 两个日期共同决定。数组顺序不影响检查，公开预期数组是子集，但空数组要求确实为空。内部 SQLite 表、架构和调度实现由开发者选择。每个场景在同一状态库上用两个独立进程执行，以检查重启和重复输入；业务投影应保持一致，notifications 为数量时表示此次新建通知数（输出中的 `notifications_created`），第二次应为0，为数组时表示持久化待处理通知清单（输出中的 `notifications`，重复执行不再新增）。

场景文件最小格式（验收脚本会补上绝对 fixture_root）：

```json
{
  "name": "retroactive",
  "run_id": "run-retroactive",
  "sources": ["/rule/v1", "/rule/v2"],
  "options": {"effective_on": "2026-11-05", "known_on": "2026-11-11"},
  "fixture_root": "fixtures"
}
```

### 版本化规则与载体

规则身份是 `rule + version`。条目字段 `rule/version/published_at/effective_at/format/text/scope` 被解析入状态库；`published_at`（可知时间）与 `effective_at`（业务生效时间）分列存储，不互相推导。正文、范围、生效日相同而仅 `format`（html→attachment 等载体迁移）变化时不新增 `rule_versions` 行，而是向 `carriers` 追加一条载体记录，因此同一规则版本可同时持有两个 carriers。全部业务表只追加、不删除：任何采集失败都不能抹掉历史依据。

### 来源观察 result 语义（observations）

- 200：`captured`，条目归档为版本与载体，并按路由/ETag 更新缓存；
- 304：已能在 carriers/source_cache 中找到同规则同版本缓存时为 `unchanged`（确认无变化）；找不到时为 `unknown_cache`，表示“尚未抓到更新/缓存无法核实”，不得当作无变化；
- 503：`unavailable`，来源暂时不可用，只记录观察，既有版本与载体原样保留；
- 其他状态：`error`。

### 历史选取与追溯生效

`selected.version` 取同时满足 `published_at <= known_on` 且 `effective_at <= effective_on` 的最高版本。追溯生效（effective_at 早于 published_at）的规则在其发布之前不参与任何历史选取，即使生效日更早；切换 known_on 即可复现某次审核当时的判断依据。

### 变更通知与审核引用

新版本追溯生效时（版本号高于审核引用、effective_at 不晚于审核日、且已在 known_on 前发布），对新版本 scope 覆盖类目的商品生成待处理通知，写入 `notifications`（字段含 sku、rule、version、reason=`retroactive_effective`、status=pending）。通知按 `sku+rule+version` 唯一约束去重，重复进程不产生新通知；范围外商品（如 adult 类目的 ADULT-1）不通知。既有 `audits` 引用只追加、永不回写，AUDIT-1 始终保留 rule_version=1。

### 规则版本查询用法

合规人员复现“某次审核发生时的规则版本”时，对同一状态库切换可知日期查询，不重新采集：

```sh
# 11月6日视角：v2 尚未发布（published_at=11-10），即使追溯生效也不可选
python3 -m collector_07.cli --database collector_07.sqlite3 \
  --query CERT-C --effective-on 2026-11-05 --known-on 2026-11-06
# 11月11日视角：v2 已发布且追溯生效，选取版本变为 2
python3 -m collector_07.cli --database collector_07.sqlite3 \
  --query CERT-C --effective-on 2026-11-05 --known-on 2026-11-11
```

输出含 `version`（选取结果，无可选版本时为 null）、`eligible_versions`（当时可知且生效）与 `excluded_versions`（附排除原因 `published_after_known_on` / `effective_after_effective_on`），可据此区分“尚未抓到更新”（版本不在库中或被 published_after_known_on 排除）与“确认没有变化”（304+缓存命中的 unchanged）。

### 回归测试

`tests/test_workflow.py` 覆盖：载体迁移不产生新版本且保留两个 carriers、缓存丢失的 304 记 unknown_cache、命中缓存的 304 记 unchanged、503 记 unavailable 且版本不被删除、追溯生效通知仅生成一次且按范围筛选、audits 引用不被回写、known_on/effective_on 双日期选取、重复进程投影一致。原始 `--capture` 入口与基线测试保持不变。

## 验收与正式展示

```sh
python3 acceptance/check.py --contracts-only
python3 acceptance/check.py
```

contracts-only 仅检查材料结构，不能当成功能验收。第二条在初始版本会因尚无 --run 而失败，这是待开发功能的验收入口，不属于基线测试。完成开发后应四个场景均 PASS；每个场景独立临时目录，执行两次、每次进程超时5秒，八次总预算40秒，无固定 sleep。正式展示从干净状态执行基线测试和第二条命令，预算小于90秒；依赖和镜像预先准备，不能用旧日志代替运行。请另加参数变化及边界回归，不修改预期来迁就实现。
