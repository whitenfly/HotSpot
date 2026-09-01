# HotSpot 热点数据服务

FastAPI 服务：多数据源热点获取 -> 清洗 -> AI 整理 -> 存储备份 -> 发布 API。
供 Firefly 项目热点榜单模块消费。

## 快速启动（Docker）

```bash
# 构建并启动
docker compose up -d --build

# 访问 Web 管理界面
#   http://localhost:3456/
```

## 配置

- 首次启动自动生成 `data/config.json`（数据源、AI 参数、调度参数）
- 访问 Web 页面「配置」标签页即可查看/修改（AI 接口地址、密钥、模型、提示词等）
- 提示词文件位于 `prompts/*.md`，可直接编辑或通过 Web「提示词」标签页修改
- 环境变量：`HOTSPOT_PORT`（默认 3456）、`HOTSPOT_DATA_DIR`、`HOTSPOT_PROMPTS_DIR`、`HOTSPOT_RETENTION`

## 对外 API（供 Firefly 消费）


| 接口                       | 说明                                     |
| -------------------------- | ---------------------------------------- |
| `GET /api/hotspot/publish` | 对外发布载荷（Firefly 热点榜单模块消费） |
| `GET /api/hotspot/ai`      | 最新 AI 整理数据（含总榜 + 各领域榜单）  |
| `GET /api/hotspot/sources` | 数据源可用性                             |
| `GET /api/hotspot/raw`     | 最新原始数据                             |
| `GET /api/hotspot/cleaned` | 最新清洗后数据                           |
| `GET /api/hotspot/health`  | 健康检查                                 |

控制接口（Web 页面调试用）：
`POST /api/hotspot/fetch`（获取数据）、`POST /api/hotspot/clean`（清洗）、
`POST /api/hotspot/ai`（AI 整理）、`POST /api/hotspot/run-all`（全流程）、
`GET/PUT /api/hotspot/config`（配置）、`GET/PUT /api/hotspot/prompts/{name}`（提示词）。

数据源管理接口（Web「数据源管理」标签页）：
`GET/POST /api/hotspot/sources/config`（列表/新增）、
`GET/PUT/DELETE /api/hotspot/sources/config/{id}`（单源增删改查）、
`POST /api/hotspot/sources/config/{id}/toggle`（卡片即时启用/禁用）、
`POST /api/hotspot/sources/test/{id}`（测试联通+数据接收预览）、
`POST /api/hotspot/sources/fetch/{id}`（单源单独获取+部分合并更新）。

RSSHub 全局实例管理接口（Web「数据源管理」→「RSSHub 全局实例」面板）：
`GET /api/hotspot/rsshub/instances`（实例列表，含 Location/Maintainer/online）、
`PUT /api/hotspot/rsshub/instances`（整体更新实例清单）、
`POST /api/hotspot/rsshub/test/{url}`（测试单实例在线状态，结果写回）。

## Firefly 接入

在 Firefly 中设置环境变量或修改 `src/config/siteConfig.ts`：

```ts
trending: { apiBaseUrl: "http://<hotspot-host>:3456" }
```

HotSpot 已开启 CORS，Firefly 前端可直接跨域请求。

## 目录结构

```
HotSpot/
├── app/          # 服务代码（config/models/fetchers/cleaner/ai_organizer/storage/pipeline/api）
├── web/          # Web 管理界面（静态页）
├── prompts/      # AI 提示词（可直接编辑）
├── data/         # 运行数据（快照备份 + latest 实时副本，可挂载持久化）
├── package/      # 打包产物（镜像包 + docker-compose）
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 数据存储（data/）

- `raw/{runId}.json` 原始数据快照（时间戳备份）
- `cleaned/{runId}.json` 清洗后数据快照
- `ai/{runId}.json` AI 整理后数据快照
- `sources/{id}.json` 数据源单文件配置（独立管理，含解析模板）
- `disabled/{源id}/{时间戳}.json` 被禁用源的数据备份（含获取时间，可恢复）
- `rsshub_instances.json` RSSHub 全局实例清单（含 Location/Maintainer/online）
- `rsshub_success.json` RSSHub 路由级成功实例记忆（{path: {instance, updatedAt}}）
- `latest/` 最新数据（供实时调用）
- `statuses/` 数据源可用性快照
- `config.json` 运行配置（调度/AI/清洗参数；数据源已独立为单文件）
- 镜像内置 `data_templates/`：首次启动自动将 42 个 RSSHub 路由源与 19 个公共实例写入数据卷（升级部署时若 `sources/` 已存在则不覆盖）
- 每次获取后自动保存原始与清洗数据；AI 整理后自动发布到 API

## 数据源管理

每个数据源独立存储于 `data/sources/{id}.json`，可通过 Web「数据源管理」标签页或直接编辑文件管理：

- **单文件配置**：含 常规参数（name/type/domain/enabled/limit/minIntervalMinutes/timeoutSeconds/config）与 **解析模板**（template）
- **解析模板**：描述如何从原始响应提取结构化条目，支持 `json`（点路径）/ `rss` / `html`（CSS 选择器）三种类型，模板字段映射 title/url/heat/publishedAt/summary/extra
- **测试联通**：Web 页面点击「测试联通」，实时请求一次并可视化展示 原始响应预览 + 解析结果表格（数据接收验证）
- **单独获取（部分更新）**：Web 页面点击「单独获取」，仅重新抓取该源数据并合并入最近快照——其他源数据保留不变，后续 清洗/AI 流程基于合并后的完整数据继续（某源失败后可单独重试，不影响整体流程）
- **启用/禁用数据隔离**：卡片开关禁用某源时，自动从最新原始快照剔除该源数据（不再进入后续清洗/AI），并将该源数据按时间戳备份到 `data/disabled/{源id}/`（保留最近 5 份，含获取时间）；重新启用时自动从备份恢复该源数据（若快照中已有更新的该源数据则不覆盖），无需重新抓取
- **增删改查**：Web 页面新增/编辑/删除/启停任一数据源（卡片上的开关可即时启停）
- **领域筛选**：Web「数据源管理」页可按领域筛选数据源（时政/社会/财经/科技/AI/游戏等 24 类）

### RSSHub 数据源与实例

- **内置 42 个 RSSHub 路由数据源**（初始禁用，按需启用）：微博热搜/知乎热榜/B站排行榜/澎湃/财新/财联社/华尔街见闻/雪球/GitHub Trending/掘金/IT之家/游民星空/NGA/arXiv/Nature 等，覆盖综合/时政/社会/国际/财经/科技/AI/互联网/娱乐/游戏/科学/学术/开源 等领域
- **全局实例管理**：`data/rsshub_instances.json` 存 19 个官方公共实例（含 Location/Maintainer），Web 页面可逐个或批量测试在线状态（online），结果写回
- **抓取优化**：RSSHub 路由抓取时**优先使用已标记 online 的实例**并按序故障转移；自动跳过返回 "Looks like something went wrong" 等错误页的实例（实例在线但该路由无数据），尽可能获取到数据
- 自定义 RSSHub 源：新增源类型选 `rsshub`，config 填 `{"path": "/xxx/yyy", "instances": ["http://本地实例:1200"]}`（instances 可选，缺省用全局实例列表）

## 许可协议

本项目遵循 [MIT license](https://mit-license.org/) 开源协议，详细查看 [LICENSE](https://github.com/whitenfly/HotSpot/blob/main/LICENSE) 文件

**版权声明：**

* Copyright (c) 2026 [whitenfly](https://github.com/whitenfly) - [HotSpot](https://github.com/whitenfly/HotSpot)

根据 MIT 开源协议，你可以自由使用、修改、分发代码，但需保留上述版权声明。
