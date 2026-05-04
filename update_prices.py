#!/usr/bin/env python3
"""
抓 qiyoujiage.com 北京 92# 油价 → 用稳定差价矩阵派生 31 省 4 油号 → fuel-prices.json。

为什么改用 qiyoujiage 替代 AKShare：
  - AKShare energy_oil_detail() 返的"最新"曾返 2022-05-17 老期（upstream stale）
  - qiyoujiage.com 实测每日同步发改委公告，2022-2026 稳定可靠
  - urllib + re 标准库，无 pip 依赖，workflow 启动快

数据派生策略（与 iOS FuelPriceTable.standardSnapshot 1:1 对齐）：
  - 北京 92# = qiyoujiage 抓取（唯一外部数据点）
  - 北京 95#/98#/0# = 92# + 标准差价（多年稳定）
  - 其他 30 省 4 油号 = 北京价 + 各省稳定差价矩阵
  - 海南附加费 +30%、西藏运输 +10% 等地理差异已编码进矩阵

参考：/Users/abaodeji/Desktop/控制器+灯控/油价自动化/update_fuel_prices.py（Android 项目同源脚本）
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# ============================================================
#  配置
# ============================================================
SOURCE_URL = "http://www.qiyoujiage.com/beijing.shtml"   # 站点 HTTPS 证书 hostname mismatch，用 http
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
TIMEOUT = 15

BEIJING_TZ = timezone(timedelta(hours=8))
OUTPUT_PATH = Path(__file__).parent / "fuel-prices.json"

# 北京 4 油号差价（对齐 FuelPriceTable.swift bj() helper）
GRADE_DELTA_95 = 0.50
GRADE_DELTA_98 = 1.54
GRADE_DELTA_0 = -0.22

# 各省相对北京的价差（元/L），多年发改委公告平均稳定值。
# **必须与 iOS FuelPriceTable.standardSnapshot.provinceDiff 完全一致**。
PROVINCE_DIFF: dict[str, tuple[float, float, float, float]] = {
    # province: (Δp92, Δp95, Δp98, Δp0)
    "北京":   (0.00, 0.00, 0.00, 0.00),
    "上海":   (-0.04, -0.04, -0.04, -0.05),
    "天津":   (-0.05, -0.05, -0.05, -0.06),
    "重庆":   (0.13, 0.13, 0.13, 0.10),
    "河北":   (-0.04, -0.04, -0.04, -0.06),
    "山西":   (-0.02, -0.02, -0.02, -0.04),
    "辽宁":   (-0.05, -0.05, -0.05, -0.07),
    "吉林":   (0.00, 0.00, 0.00, -0.02),
    "黑龙江": (-0.02, -0.02, -0.02, -0.04),
    "江苏":   (-0.04, -0.04, -0.04, -0.05),
    "浙江":   (-0.04, -0.04, -0.04, -0.05),
    "安徽":   (-0.02, -0.02, -0.02, -0.04),
    "福建":   (-0.04, -0.04, -0.04, -0.06),
    "江西":   (-0.01, -0.01, -0.01, -0.04),
    "山东":   (-0.04, -0.04, -0.04, -0.05),
    "河南":   (-0.03, -0.03, -0.03, -0.05),
    "湖北":   (0.05, 0.05, 0.05, 0.02),
    "湖南":   (0.07, 0.07, 0.07, 0.04),
    "广东":   (0.05, 0.05, 0.05, 0.02),
    "广西":   (0.10, 0.10, 0.10, 0.07),
    "海南":   (1.45, 1.50, 1.55, 1.40),    # 海南燃油附加费 ~30% 加成
    "四川":   (0.13, 0.13, 0.13, 0.10),
    "贵州":   (0.13, 0.13, 0.13, 0.10),
    "云南":   (0.13, 0.13, 0.13, 0.10),
    "陕西":   (0.00, 0.00, 0.00, -0.02),
    "甘肃":   (0.00, 0.00, 0.00, -0.02),
    "青海":   (0.05, 0.05, 0.05, 0.03),
    "宁夏":   (-0.04, -0.04, -0.04, -0.06),
    "新疆":   (-0.10, -0.10, -0.10, -0.12),
    "内蒙古": (-0.04, -0.04, -0.04, -0.06),
    "西藏":   (0.85, 0.85, 0.85, 0.80),    # 西藏运输高
}


# ============================================================
#  抓取
# ============================================================
def fetch_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_beijing_92(html: str) -> Optional[float]:
    """qiyoujiage.com 页面结构（实测 2026-05 稳定）：<dt>北京92#汽油</dt><dd>8.46</dd>"""
    patterns = [
        r"<dt>\s*北京\s*92\s*[#号]?\s*汽油\s*</dt>\s*<dd>\s*(\d+\.\d{1,3})\s*</dd>",
        r"北京\s*92\s*[#号]?\s*汽油\s*</dt>\s*<dd>\s*(\d+\.\d{1,3})",
        r"92\s*[#号]?\s*汽油.{0,80}?(\d+\.\d{1,3})\s*元\s*/?\s*升",
    ]
    for pat in patterns:
        m = re.search(pat, html, flags=re.DOTALL)
        if m:
            try:
                p = float(m.group(1))
                if 4.5 <= p <= 14.0:   # 物理边界（2022-2026 历史范围 5.5-12，留余量）
                    return p
            except ValueError:
                continue
    return None


def parse_adjustment_date(html: str) -> Optional[str]:
    """抽取页面里"最近调价日"作为 effectiveDate。抽不到用今天日期。"""
    patterns = [
        r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日",
        r"(\d{4})-(\d{1,2})-(\d{1,2})",
    ]
    today = datetime.now(BEIJING_TZ).date()
    for pat in patterns:
        m = re.search(pat, html)
        if m:
            try:
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                got = datetime(y, mo, d).date()
                # 合理性检查：仅接受最近 90 天内日期（防止抓到无关历史日期）
                if 0 <= (today - got).days <= 90:
                    return got.strftime("%Y-%m-%d")
            except (ValueError, OverflowError):
                continue
    return None


# ============================================================
#  31 省派生
# ============================================================
def build_31_province_prices(p92_beijing: float) -> dict:
    """从北京 92# 价 → 31 省 4 油号完整价格（与 iOS standardSnapshot 1:1 对齐）。"""
    p95_bj = p92_beijing + GRADE_DELTA_95
    p98_bj = p92_beijing + GRADE_DELTA_98
    p0_bj = p92_beijing + GRADE_DELTA_0

    result: dict[str, dict[str, float]] = {}
    for province, (d92, d95, d98, d0) in PROVINCE_DIFF.items():
        result[province] = {
            "p92": round(p92_beijing + d92, 2),
            "p95": round(p95_bj + d95, 2),
            "p98": round(p98_bj + d98, 2),
            "p0": round(p0_bj + d0, 2),
        }
    return result


# ============================================================
#  主流程
# ============================================================
def load_existing() -> dict:
    if not OUTPUT_PATH.exists():
        return {"version": 1, "entries": []}
    try:
        with OUTPUT_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        print("[warn] 已有 fuel-prices.json 解析失败，重新生成", file=sys.stderr)
        return {"version": 1, "entries": []}


def main() -> int:
    print(f"[update] start {datetime.now(timezone.utc).isoformat()}", file=sys.stderr)

    # 1. 抓 qiyoujiage 北京 92# 价
    print(f"[fetch] {SOURCE_URL}", file=sys.stderr)
    try:
        html = fetch_text(SOURCE_URL)
    except Exception as exc:
        print(f"[error] 抓 qiyoujiage 失败: {exc}", file=sys.stderr)
        return 1

    p92 = parse_beijing_92(html)
    if p92 is None:
        print("[error] 解析北京 92# 价失败。HTML head 500 字节:", file=sys.stderr)
        print(html[:500], file=sys.stderr)
        return 1

    eff_date = parse_adjustment_date(html) or datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
    print(f"[fetch] effectiveDate={eff_date}, beijing p92={p92}", file=sys.stderr)

    # 2. 派生 31 省 4 油号
    prices_31 = build_31_province_prices(p92)
    new_entry = {"effectiveDate": eff_date, "prices": prices_31}

    # 3. 合并到现有 fuel-prices.json（幂等去重）
    feed = load_existing()
    entries: list[dict] = feed.get("entries", [])
    existing_dates = {e.get("effectiveDate") for e in entries}

    if eff_date in existing_dates:
        # 同日 entry 已存在 → 检查北京 92# 是否变化
        for e in entries:
            if e.get("effectiveDate") == eff_date:
                old_p92 = e.get("prices", {}).get("北京", {}).get("p92")
                if old_p92 is not None and abs(old_p92 - p92) < 0.005:
                    print(f"[skip] {eff_date} 已存在且 p92={p92} 一致", file=sys.stderr)
                else:
                    e["prices"] = prices_31
                    print(f"[update] {eff_date} p92 改变: {old_p92} → {p92}", file=sys.stderr)
                break
    else:
        entries.append(new_entry)
        entries.sort(key=lambda x: x.get("effectiveDate", ""))
        print(f"[append] new entry: {eff_date}", file=sys.stderr)

    feed["version"] = 1
    feed["generatedAt"] = datetime.now(timezone.utc).isoformat()
    feed["entries"] = entries

    OUTPUT_PATH.write_text(
        json.dumps(feed, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"[done] wrote {OUTPUT_PATH}, total entries: {len(entries)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
