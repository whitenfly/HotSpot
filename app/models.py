"""数据模型定义：贯穿 获取 -> 清洗 -> AI整理 全链路的结构化数据契约。

所有快照（raw/cleaned/ai）共享一致的顶层结构：
{
  "type": "raw" | "cleaned" | "ai",
  "runId": "20260831-191000-abc",
  "fetchedAt": 1725100000000,
  "items": [...],
  "sources": [SourceStatus, ...]   # 数据源可用性
}
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field


def new_run_id() -> str:
    """生成一次数据运行（获取/清洗/AI整理）的标识：时间戳 + 短随机串。"""
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


def now_ms() -> int:
    return int(time.time() * 1000)


# ============ 数据源可用性 ============

class SourceStatus(BaseModel):
    """数据源可用性快照（需求二：可用性数据生成与数据源管理）。"""

    source: str = Field(description="数据源 id")
    sourceName: str = Field(description="数据源展示名")
    connected: bool = Field(default=False, description="本次连通性（是/否）")
    itemCount: int = Field(default=0, description="本次获取数据量")
    fetchedAt: int = Field(default=0, description="数据更新时间（获取该数据的时间，毫秒）")
    durationMs: int = Field(default=0, description="本次请求耗时（毫秒）")
    skipped: bool = Field(default=False, description="因间隔未到而复用缓存，本次未真正请求")
    heat: float | None = Field(default=None, description="数据源热度（若有，如来源平均热度）")
    heatUnit: str | None = Field(default=None, description="热度单位说明")
    error: str | None = Field(default=None, description="失败原因")


# ============ 原始数据（需求一） ============

class RawItem(BaseModel):
    """从数据源获取的单条热点条目。"""

    id: str = Field(description="条目唯一 id（源内稳定）")
    title: str = Field(description="标题")
    url: str = Field(default="", description="原文链接")
    source: str = Field(description="数据源 id")
    sourceName: str = Field(description="数据源展示名")
    domain: str | None = Field(default=None, description="数据源自带领域信息（若有）")
    heat: float | None = Field(default=None, description="数据源自带热度信息（若有）")
    heatUnit: str | None = Field(default=None, description="热度单位说明")
    publishedAt: int | None = Field(default=None, description="条目发布时间（毫秒，若有）")
    summary: str | None = Field(default=None, description="条目简介（若有）")
    extra: dict[str, Any] = Field(default_factory=dict, description="源特有扩展字段")
    fetchedAt: int = Field(default_factory=now_ms, description="数据获取时间（毫秒）")


class RawSnapshot(BaseModel):
    """一次数据获取的全部结果 + 数据源可用性。"""

    type: Literal["raw"] = "raw"
    runId: str = Field(default_factory=new_run_id)
    fetchedAt: int = Field(default_factory=now_ms)
    items: list[RawItem] = Field(default_factory=list)
    sources: list[SourceStatus] = Field(default_factory=list)


# ============ 清洗后数据（需求三） ============

class CleanedItem(BaseModel):
    """清洗后的热点条目：去除冗余信息与噪声文本，结构保留。"""

    id: str
    title: str
    url: str = ""
    source: str
    sourceName: str = ""
    domain: str | None = None
    heat: float | None = None
    heatUnit: str | None = None
    publishedAt: int | None = None
    summary: str | None = None
    fetchedAt: int = 0
    cleaned: bool = Field(default=True, description="是否经过清洗")


class CleanedSnapshot(BaseModel):
    type: Literal["cleaned"] = "cleaned"
    runId: str = Field(default_factory=new_run_id)
    cleanedFromRunId: str = Field(default="", description="清洗所基于的原始数据 runId")
    fetchedAt: int = Field(default_factory=now_ms)
    items: list[CleanedItem] = Field(default_factory=list)
    sources: list[SourceStatus] = Field(default_factory=list)


# ============ AI 整理后数据（需求四） ============

HeatLabel = Literal["viral", "top", "hot", "trending", "normal"]


class AiItem(BaseModel):
    """AI 整理后的热点条目：分类、合并、摘要、热度。"""

    id: str = Field(description="整理后条目 id（合并簇的稳定标识）")
    title: str = Field(description="合并后统一标题")
    url: str = Field(default="", description="主条目原文链接")
    sources: list[str] = Field(default_factory=list, description="合并后涉及的全部数据源 id")
    sourceNames: list[str] = Field(default_factory=list, description="合并后涉及的数据源展示名")
    categories: list[str] = Field(default_factory=list, description="AI 归类的领域（可多个）")
    summary: str = Field(default="", description="AI 整理摘要（简明扼要）")
    heat: float = Field(default=0.0, description="归一化热度 [0,1]（用于排序）")
    heatLabel: HeatLabel = Field(default="normal", description="热度等级标签")
    rank: int = Field(default=0, description="总榜单排名")
    categoryRanks: dict[str, int] = Field(default_factory=dict, description="各领域榜单中的排名")
    rawHeats: dict[str, float] = Field(default_factory=dict, description="各数据源的原始热度")
    publishedAt: int | None = Field(default=None, description="事件最早发布时间")
    updatedAt: int = Field(default_factory=now_ms, description="本次整理更新时间")
    rawItemIds: list[str] = Field(default_factory=list, description="构成该条目的原始条目 id")
    sourceCount: int = Field(default=0, description="贡献数据源数量")


class AiSnapshot(BaseModel):
    """AI 整理后的最终数据：榜单 + 各领域榜单 + 数据源可用性。"""

    type: Literal["ai"] = "ai"
    runId: str = Field(default_factory=new_run_id)
    cleanedFromRunId: str = Field(default="", description="AI 整理所基于的清洗数据 runId")
    fetchedAt: int = Field(default_factory=now_ms, description="本快照生成时间")
    generatedAt: int = Field(default_factory=now_ms, description="榜单生成时间")
    model: str = Field(default="", description="AI 模型名")
    total: int = Field(default=0, description="整理后总条目数")
    sourceItemCount: int = Field(default=0, description="AI 整理输入的清洗条目数")
    categories: list[str] = Field(default_factory=list, description="出现的全部领域")
    items: list[AiItem] = Field(default_factory=list, description="全部整理后条目（按总热度降序）")
    ranking: dict[str, list[str]] = Field(
        default_factory=dict,
        description="榜单 id 序列：overall 为总榜单，其余 key 为领域榜单",
    )
    sources: list[SourceStatus] = Field(default_factory=list, description="数据源可用性")


# ============ 对外发布 API 载荷（需求六/七，Firefly 消费） ============

class PublishPayload(BaseModel):
    """Firefly 热点榜单模块消费的最终数据格式。"""

    generatedAt: int
    updatedAt: int
    nextRefresh: int = Field(default=0, description="建议下次刷新时间")
    refreshIntervalHours: float = Field(default=0)
    sourceItemCount: int = 0
    total: int = 0
    categories: list[str] = Field(default_factory=list)
    items: list[AiItem] = Field(default_factory=list, description="总榜单条目（热度降序）")
    ranking: dict[str, list[str]] = Field(default_factory=dict)
    sources: list[SourceStatus] = Field(default_factory=list, description="数据源可用性")
    sourcesOfItems: list[SourceStatus] = Field(default_factory=list, description="供前端展示的源状态")
