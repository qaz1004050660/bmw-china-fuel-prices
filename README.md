# BMW China Fuel Prices Feed

JingX 控制器 iOS App 的油价自动更新数据源。

## 工作原理

```
GitHub Actions cron (周二/四/六 09:00 北京)
  → AKShare Python 抓发改委 31 省 × 4 油号
  → 合并到 fuel-prices.json (幂等去重)
  → git push 回 main 分支
  → jsDelivr CDN 自动镜像
  → iOS App 启动时拉取（7 天节流）
```

## 部署步骤

### 1. 创建 public GitHub repo

例：`<你的用户名>/bmw-china-fuel-prices`，**必须 public**（jsDelivr 才能镜像）。

### 2. 复制全部文件到新 repo

把这个 `tools/fuel-price-feed/` 目录里的所有内容（含 `.github/`）复制到新 repo 根目录。

```
新 repo 根/
├── .github/
│   └── workflows/
│       └── update-fuel-prices.yml
├── update_prices.py
├── requirements.txt
├── fuel-prices.json     ← 初始为空，第一次跑会填
├── README.md
└── .gitignore
```

`git push` 到 main 分支。

### 3. 启用 GitHub Actions

进入 repo 的 `Actions` tab → 点 `I understand my workflows, go ahead and enable them`。

### 4. 首次手动触发

`Actions` tab → 选 `Update Fuel Prices` workflow → 右侧 `Run workflow` → `Run workflow`。

约 30 秒后 fuel-prices.json 会被首次填充并 commit。

### 5. 通知 iOS 端

把 `<你的用户名>/<repo 名>` 告诉 iOS 开发者，他们会更新 `FuelPriceFeedClient.swift` 的 `feedURLs`：

```swift
private let feedURLs: [URL] = [
    URL(string: "https://cdn.jsdelivr.net/gh/<USERNAME>/<REPO>@main/fuel-prices.json")!,
    URL(string: "https://raw.githubusercontent.com/<USERNAME>/<REPO>/main/fuel-prices.json")!,
]
```

## 验证

部署后访问以下 URL，应能拿到 JSON：

```
https://cdn.jsdelivr.net/gh/<USERNAME>/<REPO>@main/fuel-prices.json
https://raw.githubusercontent.com/<USERNAME>/<REPO>/main/fuel-prices.json
```

## 维护

零维护。GitHub Actions 自动跑：
- 周二、四、六各 1 次（覆盖发改委每 10 工作日的调价频率）
- 抓到新数据 → 自动 commit
- 无新数据 → noop

成本：免费（GitHub Actions 月 ~5 分钟，远低于 2000 分钟免费额度）。

## 故障排查

- **Actions 红色失败**：进 workflow 看日志，常见原因：AKShare 接口字段改名、网络超时
- **iOS 端读不到新数据**：客户端 7 天节流，强制刷新可清 UserDefaults `fuel_prices_last_fetched_at`
- **jsDelivr 在中国大陆访问慢**：客户端有 fallback 到 `raw.githubusercontent.com` 自动重试

## JSON Schema

```json
{
  "version": 1,
  "generatedAt": "2026-05-06T01:00:00Z",
  "entries": [
    {
      "effectiveDate": "2026-05-06",
      "prices": {
        "北京": { "p92": 7.92, "p95": 8.43, "p98": 9.08, "p0": 7.52 },
        "重庆": { "p92": 7.85, "p95": 8.36, "p98": 9.01, "p0": 7.45 },
        "...": "31 省"
      }
    }
  ]
}
```

省份 key 用**中文短名**（去掉"省/市/自治区"后缀），与 iOS `FuelPriceTable.normalizeProvinceName` 输出一致。

## License

数据来自国家发改委公开发布，公共领域。代码 MIT。
