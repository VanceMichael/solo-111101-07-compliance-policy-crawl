import argparse
import json
from .store import RecordStore
from .capture import capture
from .workflow import Scenario, query_rule, run_scenario


def main(argv=None):
    parser = argparse.ArgumentParser(description='产品合规规则版本化采集')
    parser.add_argument("--database", default="collector.sqlite3")
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--capture", metavar="FIXTURE_ROOT")
    parser.add_argument("--route", default='/rule/v1')
    parser.add_argument("--observed-at", default="2026-11-11T10:00:00Z")
    parser.add_argument("--run", metavar="SCENARIO", dest="run",
                        help="按场景文件执行规则版本化采集工作流，并把业务投影写入 --output")
    parser.add_argument("--output", metavar="OUTPUT_JSON",
                        help="--run 业务结果输出文件（selected/versions/carriers/notifications/audits/observations）")
    parser.add_argument("--query", metavar="RULE_ID",
                        help="查询规则在某审核日与可知日下的选取版本（配合 --effective-on/--known-on）")
    parser.add_argument("--effective-on", help="查询参数：审核发生日 YYYY-MM-DD")
    parser.add_argument("--known-on", help="查询参数：可知日期 YYYY-MM-DD")
    args = parser.parse_args(argv)
    if args.capture:
        print(json.dumps(capture(args.capture, args.route, args.database, args.observed_at), ensure_ascii=False))
        return 0
    if args.run:
        if not args.output:
            parser.error("--run 需要同时提供 --output")
        scenario = Scenario.load(args.run)
        result = run_scenario(scenario, args.database)
        with open(args.output, "w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
        print(json.dumps({
            "run_id": result["run_id"],
            "versions": result["versions"],
            "carriers": result["carriers"],
            "notifications_created": result["notifications_created"],
            "selected": result["selected"],
            "output": args.output,
        }, ensure_ascii=False))
        return 0
    if args.query:
        if not args.effective_on or not args.known_on:
            parser.error("--query 需要同时提供 --effective-on 和 --known-on")
        print(json.dumps(
            query_rule(args.database, args.query, args.effective_on, args.known_on),
            ensure_ascii=False, indent=2,
        ))
        return 0
    if args.init:
        store = RecordStore(args.database)
        store.initialize()
        print(json.dumps({"stored": store.count()}))
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
