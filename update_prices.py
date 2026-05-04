#!/usr/bin/env python3
"""
抓发改委 31 省 4 油号当期油价 → 生成 fuel-prices.json。

数据源：AKShare `energy_oil_detail` —— 开源、稳定、抓发改委公告原始数据。
输出格式：与 iOS 端 FuelPriceTable.AdjustmentEntry 解码兼容。

合并策略：
  - 已有 fuel-prices.json → 读出 entries
  - 抓最新一期 → 若日期不在 entries 内则 append
  - 写回 JSON（按日期升序）

幂等：同一天反复跑不会重复入库（按 effectiveDate 去重）。
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import akshare as ak  # type: ignore


# **省名归一**（与 iOS FuelPriceTable.normalizeProvinceName 1:1 对齐）：
# 长后缀优先 strip，匹配一次就 break。
# AkShare 返的省份字段可能是"北京"/"北京市"/"广西壮族自治区"任意形式，统一去后缀。
PROVINCE_SUFFIXES = [
    "维吾尔自治区",  # 新疆
    "壮族自治区",    # 广西
    "回族自治区",    # 宁夏
    "自治区",        # 内蒙古 / 西藏
    "省",
    "市",
]

# 已知有效省份白名单（AkShare 返的脏数据 / 港澳台过滤）
VALID_PROVINCES = {
    "北京", "天津", "河北", "山西", "内蒙古",
    "辽宁", "吉林", "黑龙江",
    "上海", "江苏", "浙江", "安徽", "福建", "江西", "山东",
    "河南", "湖北", "湖南",
    "广东", "广西", "海南",
    "重庆", "四川", "贵州", "云南", "西藏",
    "陕西", "甘肃", "青海", "宁夏", "新疆",
}


def normalize_province(name: str) -> str | None:
    """与 iOS FuelPriceTable.normalizeProvinceName 一致的省份归一。"""
    n = (name or "").strip()
    for suffix in PROVINCE_SUFFIXES:
        if n.endswith(suffix):
            n = n[: -len(suffix)]
            break
    return n if n in VALID_PROVINCES else None


OUTPUT_PATH = Path(__file__).parent / "fuel-prices.json"


def fetch_latest_entries() -> dict:
    """
    AKShare energy_oil_detail() 返回 31 省 × 4 油号的最新一期。

    返回 {"YYYY-MM-DD": {"BJ": {"p92": 7.92, ...}, ...}}
    """
    df = ak.energy_oil_detail()
    if df is None or df.empty:
        raise RuntimeError("AKShare 返回空数据")

    print(f"[fetch] columns: {list(df.columns)}", file=sys.stderr)
    print(f"[fetch] rows: {len(df)}", file=sys.stderr)

    # AKShare 列名（实测）：日期, 省份, 89号, 92号, 95号, 0号
    # 不同时期接口可能有 89/92/95/98/0 不同组合，做兼容处理
    by_date: dict[str, dict[str, dict[str, float]]] = {}

    for _, row in df.iterrows():
        date_raw = str(row.get("日期", "")).strip()
        if not date_raw:
            continue
        # AKShare 日期可能是 "2026-05-06" 或 datetime
        try:
            dt = datetime.fromisoformat(date_raw[:10])
            date = dt.strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            continue

        province_raw = str(row.get("省份", "")).strip()
        province = normalize_province(province_raw)
        if not province:
            continue

        def safe(field_name: str, fallback: float = 0.0) -> float:
            val = row.get(field_name)
            try:
                return float(val) if val is not None else fallback
            except (ValueError, TypeError):
                return fallback

        # 字段优先级：92 必有，95/98/0 缺失填 0（iOS 端按 0 当 unavailable 处理）
        prices = {
            "p92": safe("92号", safe("92号汽油")),
            "p95": safe("95号", safe("95号汽油")),
            "p98": safe("98号", safe("98号汽油")),
            "p0": safe("0号", safe("0号柴油")),
        }
        if prices["p92"] <= 0:
            continue  # 92 都没有 → 这条数据不可用

        by_date.setdefault(date, {})[province] = prices

    return by_date


def load_existing() -> dict:
    if not OUTPUT_PATH.exists():
        return {"version": 1, "entries": []}
    try:
        with OUTPUT_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        print("[warn] 已有 fuel-prices.json 解析失败，重新生成", file=sys.stderr)
        return {"version": 1, "entries": []}


def merge_entries(existing: list, new_by_date: dict) -> tuple[list, int]:
    """合并新数据。返回 (合并后 entries, 新增条数)。"""
    seen_dates = {e["effectiveDate"] for e in existing}
    added = 0
    for date, prices in new_by_date.items():
        if date in seen_dates:
            continue  # 已有同日期 entry → 跳过（幂等）
        existing.append({"effectiveDate": date, "prices": prices})
        added += 1
    existing.sort(key=lambda e: e["effectiveDate"])
    return existing, added


def main() -> int:
    print(f"[update] start {datetime.now(timezone.utc).isoformat()}", file=sys.stderr)

    try:
        new_data = fetch_latest_entries()
    except Exception as exc:
        print(f"[error] AKShare 抓取失败: {exc}", file=sys.stderr)
        return 1

    if not new_data:
        print("[error] 未抓到任何有效数据", file=sys.stderr)
        return 1

    feed = load_existing()
    entries = feed.get("entries", [])
    merged, added = merge_entries(entries, new_data)

    feed["version"] = 1
    feed["generatedAt"] = datetime.now(timezone.utc).isoformat()
    feed["entries"] = merged

    with OUTPUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(feed, f, ensure_ascii=False, indent=2)

    print(f"[update] done. 新增 {added} 期，总 {len(merged)} 期", file=sys.stderr)
    return 0 if added > 0 else 0  # 即使无新数据也算成功（幂等）


if __name__ == "__main__":
    sys.exit(main())
