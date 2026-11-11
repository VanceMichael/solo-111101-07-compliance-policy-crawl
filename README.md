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

场景文件含 name、run_id、sources（有序根来源）、options、fixture_root；name 只作日志标签。expected 是公开最小预期，产品不应读取它。所有来源和选项应独立支持；四个场景不能替代全部边界测试。业务结果投影：selected、versions、carriers、notifications、audits、observations（route 与 result）；所有历史查询由 effective_on 与 known_on 两个日期共同决定。数组顺序不影响检查，公开预期数组是子集，但空数组要求确实为空。内部 SQLite 表、架构和调度实现由开发者选择。每个场景在同一状态库上用两个独立进程执行，以检查重启和重复输入；业务投影应保持一致，notifications 为数量时表示此次新建通知数，第二次应为0，为数组时表示持久化待处理通知清单。

## 验收与正式展示

```sh
python3 acceptance/check.py --contracts-only
python3 acceptance/check.py
```

contracts-only 仅检查材料结构，不能当成功能验收。第二条在初始版本会因尚无 --run 而失败，这是待开发功能的验收入口，不属于基线测试。完成开发后应四个场景均 PASS；每个场景独立临时目录，执行两次、每次进程超时5秒，八次总预算40秒，无固定 sleep。正式展示从干净状态执行基线测试和第二条命令，预算小于90秒；依赖和镜像预先准备，不能用旧日志代替运行。请另加参数变化及边界回归，不修改预期来迁就实现。
