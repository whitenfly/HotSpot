# HotSpot 提示词与配置说明（需求八/九）

## 本目录文件

| 文件 | 作用 | 何时被使用 |
| --- | --- | --- |
| `system_prompt.md` | AI 整理主提示词：分类、合并、摘要、热度 | 每次「AI 整理」的第一阶段（分批处理） |
| `finalize_prompt.md` | AI 终稿提示词：跨批合并 + 全局热度归一化 | 数据被分成多批时，第二（终稿）阶段 |

两个文件均可通过：
1. Web 页面顶部「提示词」标签页在线查看与编辑（保存后立即生效）；
2. 直接编辑本目录下的 .md 文件（无需重启服务）。

## AI 接口配置位置（需求八）

- Web 页面「配置」标签页 → `ai` 区块：
  - `baseUrl`：OpenAI 兼容接口地址（如 `https://api.deepseek.com/v1`、`https://api.openai.com/v1`）
  - `apiKey`：API 密钥（保存时会脱敏显示）
  - `model`：模型名（如 `deepseek-chat`）
  - `maxTokens`：单次请求最大输出 tokens
  - `contextWindow`：模型上下文窗口
  - `batchSize`：每批喂给 AI 的条目数（条目多、模型窗口小时调小）
  - `maxItems`：AI 整理输入条数上限（数据量大时按热度截取）
- 持久化位置：`data/config.json`

## Tokens 预估（防截断，需求八）

- 系统会自动估算每批输入 tokens 与所需输出 tokens。
- 若估算的 `输入 + 输出 > contextWindow`，会明确报错并提示调大 `contextWindow` 或调小 `batchSize`。
- 若 `maxTokens` 低于每批所需输出量（按每条约 160 tokens 估算），会报错提示调大。
- 经验值：DeepSeek-V3 建议 `contextWindow=65536`、`maxTokens=8192`、`batchSize=60`；
  条目数超过 300 时建议调小 `batchSize` 或调大 `maxTokens`。

## 建议提示词调优方向

- 摘要偏长：在 `system_prompt.md` 中强调「每点不超过 40 字」。
- 领域词表不匹配：修改标准词表段落的列表。
- 合并过严/过松：调整「合并判据」段落的描述。
