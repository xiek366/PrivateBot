"""长期记忆库（Stage 7）。

每条记忆有 category + importance + 生命周期管理。
JSON 文件存储，不引入向量数据库。
检索用 difflib 文本相似度 + importance 综合排序。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, asdict, field
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 同 category 最多保留多少条
MAX_PER_CATEGORY = 50
# 去重相似度阈值
DEDUP_THRESHOLD = 0.8
# 包含关系去重的最短长度（太短容易误判）
DEDUP_MIN_CONTAIN_LEN = 6
# importance 过滤下限
MIN_IMPORTANCE = 0.4


@dataclass
class MemoryEntry:
    """单条长期记忆。"""
    id: str
    category: str       # preference / habit / profile / relationship
    content: str
    importance: float   # 0.0 - 1.0
    created_at: str     # ISO
    last_used: Optional[str]   # ISO or None
    source_msg: str = ""

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> "MemoryEntry":
        return cls(
            id=data["id"],
            category=data["category"],
            content=data["content"],
            importance=float(data["importance"]),
            created_at=data.get("created_at", ""),
            last_used=data.get("last_used"),
            source_msg=data.get("source_msg", ""),
        )


class MemoryStore:
    """长期记忆库：加载/保存/检索/去重/生命周期管理。"""

    def __init__(self, memory_dir: Optional[Path]) -> None:
        self._dir = memory_dir
        self._cache: dict[int, list[MemoryEntry]] = {}
        self._id_counter: dict[int, int] = {}  # 每用户自增计数器

    @property
    def enabled(self) -> bool:
        return self._dir is not None

    # ---------- IO ----------

    def load(self, user_id: int) -> list[MemoryEntry]:
        if user_id in self._cache:
            return self._cache[user_id]
        if self._dir is None:
            return []

        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / f"{user_id}.json"
        entries: list[MemoryEntry] = []
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                entries = [MemoryEntry.from_json(e) for e in data.get("memories", [])]
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("读取记忆文件失败 %s: %s", path, exc)

        # 初始化 id 计数器：单调不回退
        # （add() 内部也会调 load()，若无条件重置会把计数器打回 1，造成 id 冲突）
        if entries:
            max_num = max(int(e.id.split("_")[-1]) for e in entries if e.id.startswith("m_"))
            self._id_counter[user_id] = max(self._id_counter.get(user_id, 1), max_num + 1)
        else:
            self._id_counter.setdefault(user_id, 1)

        self._cache[user_id] = entries
        return entries

    def save(self, user_id: int, entries: list[MemoryEntry]) -> None:
        if self._dir is None:
            return
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            path = self._dir / f"{user_id}.json"
            path.write_text(
                json.dumps({"user_id": user_id, "memories": [e.to_json() for e in entries]},
                           ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("保存记忆文件失败 user_id=%d: %s", user_id, exc)

    # ---------- 增删 ----------

    def add(self, user_id: int, entry: MemoryEntry) -> bool:
        """添加一条记忆，内部做去重和重要性过滤。返回是否真的入库。"""
        if entry.importance < MIN_IMPORTANCE:
            return False  # 太不重要，跳过

        entries = self.load(user_id)

        # 去重：相似内容跳过
        if self._is_similar(entry.content, entries):
            logger.debug("记忆去重跳过: %s", entry.content[:40])
            return False

        entries.append(entry)

        # 生命周期：同 category 超上限时淘汰低 importance
        self._enforce_category_limit(entries)

        self._cache[user_id] = entries
        self.save(user_id, entries)
        logger.info("新记忆 user_id=%d cat=%s importance=%.2f: %s",
                    user_id, entry.category, entry.importance, entry.content[:60])
        return True

    def add_batch(self, user_id: int, candidates: list[dict]) -> int:
        """批量添加（LLM 提取后调用）。返回实际入库条数。"""
        added = 0
        for c in candidates:
            try:
                entry = MemoryEntry(
                    id=self._next_id(user_id),
                    category=c["category"],
                    content=c["content"],
                    importance=float(c.get("importance", 0.5)),
                    created_at=_now_iso(),
                    last_used=None,
                    source_msg=c.get("source_msg", ""),
                )
                if self.add(user_id, entry):
                    added += 1
            except (KeyError, ValueError) as exc:
                logger.warning("记忆候选格式错误 %s: %s", c, exc)
        return added

    # ---------- 检索 ----------

    def retrieve(self, user_id: int, query: str, top_k: int = 5) -> list[MemoryEntry]:
        """综合排序检索：importance×0.6 + 相关性×0.3 + 时间衰减×0.1。"""
        entries = self.load(user_id)
        if not entries:
            return []

        # 关键词提取（query 里的中文字符作为粗匹配）
        query_keywords = set(re.findall(r"[\u4e00-\u9fff]+", query))

        scored: list[tuple[float, MemoryEntry]] = []
        for e in entries:
            # 文本相似度
            sim = SequenceMatcher(None, query, e.content).ratio()

            # 关键词命中
            kw_hit = sum(1 for kw in query_keywords if kw in e.content)
            kw_score = min(kw_hit * 0.2, 0.4)

            # 相关性分（sim 和 kw_hit 取高）
            relevance = max(sim, kw_score)

            # 时间衰减（越新越优先，但不过度）
            recency = self._recency_score(e.created_at)

            final = e.importance * 0.6 + relevance * 0.3 + recency * 0.1
            scored.append((final, e))

        scored.sort(key=lambda x: x[0], reverse=True)

        # Top-K 中 importance >= 0.7 的必选 + relevance 高的补充
        # 简化：直接取 top_k，importance >= 0.7 优先
        high_imp = [e for _, e in scored if e.importance >= 0.7][:top_k]
        remaining = [e for _, e in scored if e.importance < 0.7][:top_k - len(high_imp)]

        result = high_imp + remaining
        # 更新 last_used
        now_iso = _now_iso()
        for e in result:
            e.last_used = now_iso
        self.save(user_id, entries)
        return result

    # ---------- 工具方法 ----------

    def _next_id(self, user_id: int) -> str:
        counter = self._id_counter.get(user_id, 1)
        self._id_counter[user_id] = counter + 1
        return f"m_{counter:04d}"

    def _is_similar(self, content: str, entries: list[MemoryEntry]) -> bool:
        for e in entries:
            if self._is_duplicate(content, e.content):
                return True
        return False

    @staticmethod
    def _is_duplicate(a: str, b: str) -> bool:
        """两条记忆是否算重复。

        判据一：difflib 相似度超阈值。
        判据二：短句被长句完整包含（且短句够长）。纯相似度会漏掉
        "原句 + 补充说明"这种最典型的重复——当长句恰为短句 1.5 倍长时，
        相似度精确等于 0.8，被 `>` 判为非相似。
        """
        if SequenceMatcher(None, a, b).ratio() > DEDUP_THRESHOLD:
            return True
        short, long = (a, b) if len(a) <= len(b) else (b, a)
        return len(short) >= DEDUP_MIN_CONTAIN_LEN and short in long

    def _enforce_category_limit(self, entries: list[MemoryEntry]) -> None:
        """同 category 超过 MAX_PER_CATEGORY 时淘汰最低 importance 的。"""
        from collections import defaultdict
        by_cat: dict[str, list[MemoryEntry]] = defaultdict(list)
        for e in entries:
            by_cat[e.category].append(e)

        for cat, cat_entries in by_cat.items():
            if len(cat_entries) > MAX_PER_CATEGORY:
                # 按 importance 升序，删最低的
                cat_entries.sort(key=lambda e: e.importance)
                excess = len(cat_entries) - MAX_PER_CATEGORY
                for e in cat_entries[:excess]:
                    entries.remove(e)

    @staticmethod
    def _recency_score(created_at: str) -> float:
        """0.0 - 1.0，越新分越高。1 天前 = 0.9, 7 天前 = 0.5, 30 天前 = 0.1"""
        try:
            created = datetime.fromisoformat(created_at)
            days = max(0.01, (datetime.now() - created).total_seconds() / 86400)
        except (ValueError, TypeError):
            return 0.5
        # 衰减：score = 1.0 / (1 + days * 0.2)
        return min(1.0, 1.0 / (1.0 + days * 0.2))


def memories_to_prompt(memories: list[MemoryEntry]) -> str:
    """把检索到的记忆转成自然语言注入 Prompt。"""
    if not memories:
        return ""
    lines = [
        "以下是你对这个用户的了解，请在对话中自然地体现出来（不要说'根据我的记忆'，要像真的记得一样）："
    ]
    for m in memories:
        lines.append(f"- {m.content}")
    return "\n".join(lines)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")
