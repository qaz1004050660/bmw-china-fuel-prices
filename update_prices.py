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
import pandas as pd  # type: ignore


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


def find_latest_date() -> str:
    """
    用 energy_oil_hist 拿调价历史，找最近一次调价日期。
    energy_oil_detail() 不传 date 默认返回 2022 旧数据，必须显式传日期。
    返回 YYYYMMDD 格式。
    """
    try:
        hist = ak.energy_oil_hist(symbol="北京")
        if hist is not None and not hist.empty:
            # hist 列：日期 / 0号 / 92号 / 95号 / 89号 等
            hist["日期"] = pd.to_datetime(hist["日期"], errors="coerce")
            hist = hist.dropna(subset=["日期"]).sort_values("日期", ascending=False)
            if not hist.empty:
                latest = hist.iloc[0]["日期"]
                return latest.strftime("%Y%m%d")
    except Exception as exc:
        print(f"[warn] energy_oil_hist 失败: {exc}", file=sys.stderr)

    # Fallback：从今天倒推找最近有数据的日期
    from datetime import date as dt_date, timedelta
    for i in range(30):
        d = (dt_date.today() - timedelta(days=i)).strftime("%Y%m%d")
        try:
            df = ak.energy_oil_detail(date=d)
            if df is not None and not df.empty:
                return d
        except Exception:
            continue
    raise RuntimeError("找不到任何有效油价日期")


def fetch_latest_entries() -> dict:
    """
    抓最新调价日 31 省 × 4 油号数据。
    返回 {"YYYY-MM-DD": {"北京": {"p92": 7.92, ...}, ...}}
    """
    target_yyyymmdd = find_latest_date()
    print(f"[fetch] target date: {target_yyyymmdd}", file=sys.stderr)
    df = ak.energy_oil_detail(date=target_yyyymmdd)
    if df is None or df.empty:
        raise RuntimeError(f"AKShare {target_yyyymmdd} 返回空数据")

    print(f"[fetch] columns: {list(df.columns)}", file=sys.stderr)
    print(f"[fetch] rows: {len(df)}", file=sys.stderr)

    # AKShare energy_oil_detail() 实测列名：
    #   日期 / 地区 / V_0 / V_89 / V_92 / V_95 / ZDE_* / QE_*
    # **不返回 98 号**（中国 92/95/89/0 是发改委标准 4 油号；98 号需省厅另行公布）
    # 我们用稳定差价 p98 = p92 + 1.54 派生，与 FuelPriceTable.bj() helper 一致。
    by_date: dict[str, dict[str, dict[str, float]]] = {}

    def safe(row, *field_names: str) -> float:
        for name in field_names:
            val = row.get(name)
            if val is None:
                continue
            try:
                f = float(val)
                if f > 0:
                    return f
            except (ValueError, TypeError):
                continue
        return 0.0

    for _, row in df.iterrows():
        date_raw = str(row.get("日期", "")).strip()
        if not date_raw:
            continue
        try:
            dt = datetime.fromisoformat(date_raw[:10])
            date = dt.strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            continue

        # 实测字段名是"地区"，兼容老接口"省份"
        province_raw = str(row.get("地区") or row.get("省份") or "").strip()
        province = normalize_province(province_raw)
        if not province:
            continue

        p92 = safe(row, "V_92", "92号", "92号汽油")
        if p92 <= 0:
            continue  # 92 都没有 → 这行数据不可用

        prices = {
            "p92": p92,
            "p95": safe(row, "V_95", "95号", "95号汽油"),
            # 98 号 AKShare 不返，按稳定差价派生：95 = 92 + 0.50, 98 = 92 + 1.54
            "p98": p92 + 1.54,
            "p0": safe(row, "V_0", "0号", "0号柴油"),
        }
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
