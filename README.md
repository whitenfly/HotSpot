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

| 接口 | 说明 |
| --- | --- |
| `GET /api/hotspot/publish` | 对外发布载荷（Firefly 热点榜单模块消费） |
| `GET /api/hotspot/ai` | 最新 AI 整理数据（含总榜 + 各领域榜单） |
| `GET /api/hotspot/sources` | 数据源可用性 |
| `GET /api/hotspot/raw` | 最新原始数据 |
| `GET /api/hotspot/cleaned` | 最新清洗后数据 |
| `GET /api/hotspot/health` | 健康检查 |

控制接口（Web 页面调试用）：
`POST /api/hotspot/fetch`（获取数据）、`POST /api/hotspot/clean`（清洗）、
`POST /api/hotspot/ai`（AI 整理）、`POST /api/hotspot/run-all`（全流程）、
`GET/PUT /api/hotspot/config`（配置）、`GET/PUT /api/hotspot/prompts/{name}`（提示词）。

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
- `latest/` 最新数据（供实时调用）
- `statuses/` 数据源可用性快照
- `config.json` 运行配置
- 每次获取后自动保存原始与清洗数据；AI 整理后自动发布到 API
