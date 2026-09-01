"""运行时配置管理：数据源、AI 参数、调度、清洗参数。

配置持久化于 data/config.json，可通过 Web UI 或直接编辑文件修改。
默认配置内置于代码（DEFAULT_*），文件缺失时自动生成。
"""

from __future__ import annotations

import json
import os
import re
import threading
from copy import deepcopy
from typing import Any

DATA_DIR = os.environ.get("HOTSPOT_DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "data"))
PROMPTS_DIR = os.environ.get("HOTSPOT_PROMPTS_DIR", os.path.join(os.path.dirname(__file__), "..", "prompts"))

CONFIG_FILE = os.path.join(DATA_DIR, "config.json")

# ============ 默认数据源定义 ============
# type: rss / json_get / json_post / html / rsshub / wbi_json
# domain: 数据源自带领域信息（用于展示；AI 整理阶段仍会重新归类）
# minIntervalMinutes: 两次真实请求的最小间隔，防止被风控（需求一）

DEFAULT_SOURCES: list[dict[str, Any]] = [
    # ---- 中文综合/社区/新闻 ----
    {"id": "weibo", "name": "微博热搜", "type": "json_get", "domain": "综合", "enabled": True,
     "limit": 50, "minIntervalMinutes": 10, "timeoutSeconds": 15,
     "config": {"url": "https://weibo.com/ajax/statuses/hot_band", "headers": {"Referer": "https://weibo.com/"}}},
    {"id": "baidu", "name": "百度热搜", "type": "json_get", "domain": "综合", "enabled": True,
     "limit": 50, "minIntervalMinutes": 10, "timeoutSeconds": 15,
     "config": {"url": "https://top.baidu.com/api/board?platform=wise&tab=realtime"}},
    {"id": "toutiao", "name": "今日头条", "type": "json_get", "domain": "综合", "enabled": True,
     "limit": 50, "minIntervalMinutes": 10, "timeoutSeconds": 15,
     "config": {"url": "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc"}},
    {"id": "zhihu", "name": "知乎热榜", "type": "json_get", "domain": "社区", "enabled": True,
     "limit": 50, "minIntervalMinutes": 10, "timeoutSeconds": 15,
     "config": {"url": "https://api.zhihu.com/topstory/hot-lists/total?limit=50"}},
    {"id": "v2ex", "name": "V2EX", "type": "json_get", "domain": "社区", "enabled": True,
     "limit": 30, "minIntervalMinutes": 10, "timeoutSeconds": 15,
     "config": {"url": "https://www.v2ex.com/api/topics/hot.json"}},
    {"id": "hupu", "name": "虎扑热帖", "type": "json_get", "domain": "体育", "enabled": True,
     "limit": 30, "minIntervalMinutes": 10, "timeoutSeconds": 15,
     "config": {"url": "https://m.hupu.com/api/v2/bbs/topicThreads?topicId=1&page=1"}},
    {"id": "thepaper", "name": "澎湃新闻", "type": "json_get", "domain": "新闻", "enabled": True,
     "limit": 30, "minIntervalMinutes": 10, "timeoutSeconds": 15,
     "config": {"url": "https://cache.thepaper.cn/contentapi/wwwIndex/rightSidebar"}},
    {"id": "netease", "name": "网易新闻", "type": "json_get", "domain": "新闻", "enabled": True,
     "limit": 40, "minIntervalMinutes": 10, "timeoutSeconds": 15,
     "config": {"url": "https://m.163.com/fe/api/hot/news/flow"}},
    {"id": "qq-news", "name": "腾讯新闻", "type": "json_get", "domain": "新闻", "enabled": True,
     "limit": 40, "minIntervalMinutes": 10, "timeoutSeconds": 15,
     "config": {"url": "https://r.inews.qq.com/gw/event/hot_ranking_list?page_size=40"}},
    {"id": "36kr", "name": "36氪热榜", "type": "json_post", "domain": "科技", "enabled": True,
     "limit": 30, "minIntervalMinutes": 10, "timeoutSeconds": 15,
     "config": {"url": "https://gateway.36kr.com/api/mis/nav/home/nav/rank/hot",
               "body": {"partner_id": "wap", "param": {"siteId": 1, "platformId": 2},
                        "timestamp": 0}}},
    {"id": "bilibili-rank", "name": "B站排行榜", "type": "wbi_json", "domain": "娱乐", "enabled": True,
     "limit": 50, "minIntervalMinutes": 15, "timeoutSeconds": 15,
     "config": {"url": "https://api.bilibili.com/x/web-interface/ranking/v2", "params": {"rid": "0", "type": "all"}}},
    {"id": "douyin", "name": "抖音热搜", "type": "rsshub", "domain": "娱乐", "enabled": True,
     "limit": 30, "minIntervalMinutes": 15, "timeoutSeconds": 20,
     "config": {"path": "/douyin/hot"}},
    # ---- 中文科技/媒体 RSS ----
    {"id": "ithome", "name": "IT之家", "type": "rss", "domain": "科技", "enabled": True,
     "limit": 30, "minIntervalMinutes": 15, "timeoutSeconds": 20,
     "config": {"url": "https://www.ithome.com/rss/"}},
    {"id": "sspai", "name": "少数派", "type": "json_get", "domain": "科技", "enabled": True,
     "limit": 30, "minIntervalMinutes": 15, "timeoutSeconds": 15,
     "config": {"url": "https://sspai.com/api/v1/article/hot/page/get?limit=30&offset=0"}},
    {"id": "linuxdo", "name": "开源中国", "type": "html", "domain": "科技", "enabled": True,
     "limit": 30, "minIntervalMinutes": 30, "timeoutSeconds": 20,
     "config": {"url": "https://www.oschina.net/news", "selector": "a[href]",
               "hrefFilter": r"^https://www\.oschina\.net/news/\d+$"}},
    {"id": "solidot", "name": "Solidot", "type": "rss", "domain": "科技", "enabled": True,
     "limit": 30, "minIntervalMinutes": 30, "timeoutSeconds": 20,
     "config": {"url": "https://www.solidot.org/index.rss"}},
    # ---- 国际 ----
    {"id": "hackernews", "name": "Hacker News", "type": "json_get", "domain": "科技", "enabled": True,
     "limit": 30, "minIntervalMinutes": 10, "timeoutSeconds": 15,
     "config": {"url": "https://hacker-news.firebaseio.com/v0/topstories.json"}},
    {"id": "reddit", "name": "Reddit", "type": "rss", "domain": "国际", "enabled": True,
     "limit": 25, "minIntervalMinutes": 15, "timeoutSeconds": 20,
     "config": {"url": "https://www.reddit.com/r/all/top.rss?t=day&limit=25"}},
    {"id": "github", "name": "GitHub", "type": "json_get", "domain": "开源", "enabled": True,
     "limit": 30, "minIntervalMinutes": 30, "timeoutSeconds": 15,
     "config": {"url": "https://api.github.com/search/repositories?q=created:%3E{since}&sort=stars&order=desc&per_page=30"}},
    {"id": "lobsters", "name": "Lobsters", "type": "rss", "domain": "科技", "enabled": True,
     "limit": 25, "minIntervalMinutes": 15, "timeoutSeconds": 20,
     "config": {"url": "https://lobste.rs/rss"}},
    {"id": "producthunt", "name": "Product Hunt", "type": "rss", "domain": "产品", "enabled": True,
     "limit": 25, "minIntervalMinutes": 30, "timeoutSeconds": 20,
     "config": {"url": "https://www.producthunt.com/feed"}},
    {"id": "techcrunch", "name": "TechCrunch", "type": "rss", "domain": "科技", "enabled": True,
     "limit": 25, "minIntervalMinutes": 30, "timeoutSeconds": 20,
     "config": {"url": "https://techcrunch.com/feed/"}},
    {"id": "the-verge", "name": "The Verge", "type": "rss", "domain": "科技", "enabled": True,
     "limit": 25, "minIntervalMinutes": 30, "timeoutSeconds": 20,
     "config": {"url": "https://www.theverge.com/rss/index.xml"}},
    {"id": "arxiv-cs-ai", "name": "arXiv·AI", "type": "rss", "domain": "学术", "enabled": True,
     "limit": 25, "minIntervalMinutes": 60, "timeoutSeconds": 25,
     "config": {"url": "https://export.arxiv.org/api/query?search_query=cat:cs.AI&sortBy=submittedDate&sortOrder=descending&max_results=30"}},
    {"id": "nature", "name": "Nature", "type": "rss", "domain": "科学", "enabled": True,
     "limit": 20, "minIntervalMinutes": 60, "timeoutSeconds": 20,
     "config": {"url": "https://www.nature.com/nature.rss"}},
    # ---- AI 厂商动态 ----
    {"id": "openai-news", "name": "OpenAI", "type": "google_news", "domain": "AI", "enabled": True,
     "limit": 15, "minIntervalMinutes": 60, "timeoutSeconds": 25,
     "config": {"query": "site:openai.com/news", "hl": "zh-CN", "gl": "CN", "ceid": "CN:zh-Hans"}},
    {"id": "anthropic-news", "name": "Anthropic", "type": "html", "domain": "AI", "enabled": True,
     "limit": 15, "minIntervalMinutes": 60, "timeoutSeconds": 20,
     "config": {"url": "https://www.anthropic.com/news", "selector": "a[href*='/news/']"}},
    {"id": "deepseek-blog", "name": "DeepSeek", "type": "google_news", "domain": "AI", "enabled": True,
     "limit": 15, "minIntervalMinutes": 60, "timeoutSeconds": 25,
     "config": {"query": "site:api-docs.deepseek.com OR site:deepseek.com", "hl": "zh-CN", "gl": "CN", "ceid": "CN:zh-Hans"}},
    {"id": "qwen-blog", "name": "通义千问 Qwen", "type": "google_news", "domain": "AI", "enabled": True,
     "limit": 15, "minIntervalMinutes": 60, "timeoutSeconds": 25,
     "config": {"query": "site:qwen.ai OR site:qwenlm.github.io", "hl": "zh-CN", "gl": "CN", "ceid": "CN:zh-Hans"}},
    # ---- 新增数据源（需求一）：大黑AI工具速报 ----
    {"id": "daheiai", "name": "大黑AI工具速报", "type": "rss", "domain": "AI工具", "enabled": True,
     "limit": 40, "minIntervalMinutes": 60, "timeoutSeconds": 30,
     "config": {"url": "https://news.daheiai.com/changelog_rss.php"}},
]

DEFAULT_AI_CONFIG: dict[str, Any] = {
    "enabled": False,
    # OpenAI 兼容接口地址：如 https://api.deepseek.com/v1 或 https://api.openai.com/v1
    "baseUrl": "https://api.deepseek.com/v1",
    "apiKey": "",
    "model": "deepseek-chat",
    "temperature": 0.3,
    "maxTokens": 8192,           # 单次请求最大输出 tokens（DeepSeek 默认上限）
    "timeoutSeconds": 600,
    "batchSize": 50,             # 每批喂给 AI 的条目数
    "maxItems": 300,             # AI 整理输入条数上限（超出截取热度靠前者）
    "contextWindow": 65536,      # 模型上下文窗口（用于估算输入+输出是否超限）
    "jsonMode": True,            # 请求 response_format=json_object
    "enableSummary": True,       # 是否让 AI 写摘要
    "enableFetchContent": False, # 摘要不足时是否允许访问原文链接补全内容
}

DEFAULT_SCHEDULER_CONFIG: dict[str, Any] = {
    "enabled": True,
    "fetchIntervalMinutes": 15,  # 数据获取周期
    "aiIntervalMinutes": 30,     # AI 整理周期（需先有清洗后数据）
    "runAiAfterFetch": False,    # 每次获取完成后是否自动触发 AI 整理
    "requestDelaySeconds": 0.5,  # 数据源之间的请求间隔（防风控）
}

DEFAULT_CLEANING_CONFIG: dict[str, Any] = {
    "stripHtml": True,           # 去除 HTML 标签
    "collapseWhitespace": True,  # 折叠连续空白
    "normalizeTitle": True,      # 标题归一化（去括号注释、多余符号）
    "dropNoTitle": True,         # 丢弃无标题条目
    "dropNoUrl": False,          # 丢弃无原文链接条目
    "dedupeWithinSource": True,  # 源内按标题去重
    "maxTitleLength": 200,
    "maxSummaryLength": 500,
}

DEFAULT_PROMPTS: dict[str, str] = {
    "system_prompt": """你是热点数据整理专家。""",  # 占位，实际以 prompts/ 目录文件为准
}


class ConfigManager:
    """配置管理器：内存缓存 + 文件持久化，线程安全。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._config: dict[str, Any] = {}
        self._prompts: dict[str, str] = {}
        self.load()

    # ---------- 加载 / 保存 ----------

    def load(self) -> None:
        with self._lock:
            os.makedirs(DATA_DIR, exist_ok=True)
            merged = self._defaults()
            if os.path.exists(CONFIG_FILE):
                try:
                    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                        user_cfg = json.load(f)
                    self._deep_merge(merged, user_cfg)
                except Exception:
                    # 配置文件损坏时保留备份并回退默认
                    try:
                        os.rename(CONFIG_FILE, CONFIG_FILE + ".corrupt")
                    except OSError:
                        pass
            self._config = merged
            self.save()
            self._load_prompts()

    @staticmethod
    def _defaults() -> dict[str, Any]:
        return {
            "version": 1,
            "scheduler": deepcopy(DEFAULT_SCHEDULER_CONFIG),
            "ai": deepcopy(DEFAULT_AI_CONFIG),
            "cleaning": deepcopy(DEFAULT_CLEANING_CONFIG),
            "sources": deepcopy(DEFAULT_SOURCES),
        }

    @staticmethod
    def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> None:
        for key, value in override.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                ConfigManager._deep_merge(base[key], value)
            else:
                base[key] = value

    def save(self) -> None:
        with self._lock:
            os.makedirs(DATA_DIR, exist_ok=True)
            tmp = CONFIG_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._config, f, ensure_ascii=False, indent=2)
            os.replace(tmp, CONFIG_FILE)

    # ---------- 读取 ----------

    def get(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._config)

    def get_sources(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            sources = deepcopy(self._config.get("sources", []))
        if enabled_only:
            sources = [s for s in sources if s.get("enabled", True)]
        return sources

    def get_source(self, source_id: str) -> dict[str, Any] | None:
        for s in self.get_sources():
            if s["id"] == source_id:
                return s
        return None

    def get_scheduler(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._config.get("scheduler", DEFAULT_SCHEDULER_CONFIG))

    def get_ai(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._config.get("ai", DEFAULT_AI_CONFIG))

    def get_cleaning(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._config.get("cleaning", DEFAULT_CLEANING_CONFIG))

    def update(self, new_config: dict[str, Any]) -> dict[str, Any]:
        """整体更新配置（Web UI 保存），浅层校验后合并并持久化。"""
        with self._lock:
            base = self._defaults()
            self._deep_merge(base, new_config)
            # 校验 sources 结构
            if "sources" in base:
                base["sources"] = [s for s in base["sources"] if isinstance(s, dict) and s.get("id")]
            self._config = base
            self.save()
            return self.get()

    # ---------- 提示词 ----------

    def _load_prompts(self) -> None:
        os.makedirs(PROMPTS_DIR, exist_ok=True)
        self._prompts = {}
        for name in ("system_prompt", "finalize_prompt", "config_guide"):
            path = os.path.join(PROMPTS_DIR, f"{name}.md")
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    self._prompts[name] = f.read()
            elif name == "system_prompt":
                self._prompts[name] = DEFAULT_PROMPTS["system_prompt"]
                self.save_prompt("system_prompt", self._prompts[name])

    def get_prompt(self, name: str) -> str:
        with self._lock:
            self._load_prompts()
            return self._prompts.get(name, "")

    def list_prompts(self) -> list[str]:
        with self._lock:
            return sorted(os.listdir(PROMPTS_DIR)) if os.path.isdir(PROMPTS_DIR) else []

    def save_prompt(self, name: str, content: str) -> None:
        if not name or not re.fullmatch(r"[A-Za-z0-9_-]+", name) or "." in name:
            raise ValueError("非法提示词文件名")
        with self._lock:
            os.makedirs(PROMPTS_DIR, exist_ok=True)
            path = os.path.join(PROMPTS_DIR, f"{name}.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            self._prompts[name] = content


config_manager = ConfigManager()
