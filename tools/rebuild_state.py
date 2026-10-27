"""清掉被污染的去重基线，只保留能确认属于目标博主的历史作品。

背景（2026-10-27）：旧版 fetch_feed 全文扫 /video/<id> 全收，抖音主页渲染成
推荐流时把别人的视频当成监控对象的新作品写进 state，名单从 82 条涨到 523 条。
修复后的 fetch_feed 会用卡片 alt 的「昵称：」前缀 + 页面标题双重校验归属。

本脚本：备份 state.json → 用修复后的 fetch 抓一次干净列表 → 以它重建 items。
不推送任何东西；被删掉的老作品将来再露出时会走 stale 分支补录 baseline。
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "lib"
sys.path.insert(0, str(LIB))

import fetch_feed  # noqa: E402
import run_once  # noqa: E402


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    cfg = run_once.load_config()
    sp = run_once.state_path(cfg)
    st = json.loads(sp.read_text(encoding="utf-8"))

    expect = (cfg.get("user") or {}).get("nickname", "")
    feed = fetch_feed.fetch(cfg["user"]["sec_uid"],
                            budget_ms=cfg["fetch"].get("budget_ms", 25000),
                            expect_nickname=expect)
    if not feed.get("ok"):
        print(json.dumps({"ok": False, "abort": "抓取失败，state 未改动",
                          "error": feed.get("error")}, ensure_ascii=False))
        return 1

    verified = feed["items"]
    verified_ids = [it["aweme_id"] for it in verified]

    bak = sp.with_suffix(sp.suffix + ".bak-before-rebuild-%s" % time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(sp, bak)

    old_items = st.get("items", {})
    new_items: dict[str, dict] = {}
    for raw in verified:
        oid = raw["aweme_id"]
        old = old_items.get(oid) or {}
        item = run_once.build_item(raw, cfg)
        item["stage"] = "baseline"
        if old.get("first_seen"):
            item["first_seen"] = old["first_seen"]
        new_items[oid] = item

    removed = [k for k in old_items if k not in new_items]
    st["items"] = new_items
    st["user"] = feed.get("user")
    st.setdefault("baseline_at", run_once.now_iso())
    st["rebuild"] = {"at": run_once.now_iso(), "kept": len(new_items),
                     "removed": len(removed), "backup": bak.name,
                     "dropped_foreign_on_page": feed.get("dropped_foreign")}
    sp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")

    print(json.dumps({"ok": True, "kept": len(new_items), "removed": len(removed),
                      "backup": bak.name, "owner": feed.get("user", {}).get("nickname"),
                      "videos_on_page": feed.get("videos_on_page"),
                      "dropped_foreign": feed.get("dropped_foreign"),
                      "kept_ids": verified_ids}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
