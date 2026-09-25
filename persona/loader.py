"""人设加载与 prompt 拼装 + 参考语料轻量检索（Stage 5）。

Stage 3 功能：热重载人设 YAML，mtime + size 监测。
Stage 5 功能：参考语料 chunk 预处理 + 每次对话时轻量检索 Top K 相关片段。
"""
from __future__ import annotations

import difflib
import logging
import re
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

_ROLE_RULE = (
    "你必须始终保持这个身份和说话风格，任何情况下都不要跳出角色，"
    "不要提及你是 AI、助手或语言模型。"
)

# Stage 5 检索参数（写死，够用；以后需要调再改成配置项）
_REFERENCE_TOP_K = 3              # 返回前几个最相关的 chunk
_REFERENCE_THRESHOLD = 0.15       # 相似度阈值，低于此不注入
_SIM_WEIGHT = 0.4                 # 文本整体相似度权重
_WORD_WEIGHT = 0.6                # 关键词重叠度权重


class PersonaLoader:
    def __init__(
        self,
        path: Optional[Path],
        fallback_prompt: str,
        style_dir: Optional[Path] = None,
    ) -> None:
        # ---- 人设（Stage 3）----
        self._path = path
        self._fallback = fallback_prompt
        self._name = ""
        self._system_prompt = fallback_prompt
        self._examples: list[dict] = []
        self._signature: Optional[tuple[float, int]] = None      # 人设文件签名
        self._failed_signature: Optional[tuple[float, int]] = None

        # ---- 参考语料（Stage 5）----
        self._style_dir = style_dir
        self._chunks: list[str] = []                               # 已切好的 chunk 列表
        self._chunk_signature: Optional[tuple[float, int]] = None  # reference.txt 签名
        self._chunk_failed_signature: Optional[tuple[float, int]] = None

        self._loaded = False

    # ---------- Stage 3：人设 ----------

    def get_system_prompt(self) -> str:
        self._refresh()
        return self._system_prompt

    def get_examples(self) -> list[dict]:
        self._refresh()
        return list(self._examples)

    def _signature_now(self) -> Optional[tuple[float, int]]:
        if self._path is None or not self._path.exists():
            return None
        stat = self._path.stat()
        return (stat.st_mtime, stat.st_size)

    def _refresh(self) -> None:
        # ---- 人设 YAML 热重载（Stage 3）----
        signature = self._signature_now()
        if signature is not None and signature == self._signature:
            pass  # 没变
        elif signature is not None and signature == self._failed_signature:
            pass  # 还是同一个坏文件，跳过
        else:
            # 需要重新加载人设
            if signature is None:
                if self._path is not None:
                    logger.warning("人设文件不存在，回退到 SYSTEM_PROMPT: %s", self._path)
                self._name = ""
                self._system_prompt = self._fallback
                self._examples = []
                self._signature = None
                self._failed_signature = None
                self._loaded = True
            else:
                try:
                    raw = yaml.safe_load(self._path.read_text(encoding="utf-8"))
                    if not isinstance(raw, dict):
                        raise ValueError("人设文件顶层必须是映射（key: value）")
                    prompt = self._build_prompt(raw)
                    examples = self._build_examples(raw)
                except (yaml.YAMLError, OSError, ValueError) as exc:
                    logger.warning("人设加载失败，保留上一份可用人设: %s", exc)
                    self._failed_signature = signature
                    self._loaded = True
                else:
                    self._name = str(raw.get("name") or "")
                    self._system_prompt = prompt
                    self._examples = examples
                    self._signature = signature
                    self._failed_signature = None
                    self._loaded = True
                    logger.info("persona loaded: %s", self._name or "(未命名)")

        # ---- 参考语料热重载（Stage 5）----
        self._refresh_chunks()

    @staticmethod
    def _build_prompt(raw: dict[str, Any]) -> str:
        identity = str(raw.get("identity") or "").strip()
        if not identity:
            raise ValueError("缺少必填字段 identity")

        sections = [f"【身份】\n{identity}"]

        personality = str(raw.get("personality") or "").strip()
        if personality:
            sections.append(f"【性格】\n{personality}")

        style = PersonaLoader._as_lines(raw.get("speaking_style"))
        if style:
            sections.append(f"【说话风格】\n{style}")

        taboos = PersonaLoader._as_lines(raw.get("taboos"))
        if taboos:
            sections.append(f"【禁忌】\n{taboos}")

        sections.append(_ROLE_RULE)
        return "\n\n".join(sections)

    @staticmethod
    def _as_lines(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, (list, tuple)):
            items = [str(item).strip() for item in value]
        else:
            items = [str(value).strip()]
        return "\n".join(f"- {item}" for item in items if item)

    @staticmethod
    def _build_examples(raw: dict[str, Any]) -> list[dict]:
        raw_examples = raw.get("examples") or []
        if not isinstance(raw_examples, list):
            return []

        messages: list[dict] = []
        for item in raw_examples:
            if not isinstance(item, dict):
                continue
            user = str(item.get("user") or "").strip()
            assistant = str(item.get("assistant") or "").strip()
            if not user or not assistant:
                continue
            messages.append({"role": "user", "content": user})
            messages.append({"role": "assistant", "content": assistant})
        return messages

    # ---------- Stage 5：参考语料 ----------

    def find_top_chunks(self, query: str, top_k: int = _REFERENCE_TOP_K) -> list[str]:
        """检索与 query 最相关的参考片段。返回空列表表示没匹配到或没有参考语料。"""
        if not self._chunks:
            return []

        query_clean = re.sub(r"[\s\u3000]+", "", query.lower())
        if not query_clean:
            return []

        query_bigrams = self._bigrams(query_clean)

        scored: list[tuple[float, str]] = []
        for chunk in self._chunks:
            chunk_clean = re.sub(r"[\s\u3000]+", "", chunk.lower())
            # 文本整体相似度
            sim = difflib.SequenceMatcher(None, query_clean, chunk_clean).ratio()
            # 2-gram 重叠度（比关键词分词更适合中文）
            chunk_bigrams = self._bigrams(chunk_clean)
            if chunk_bigrams and query_bigrams:
                overlap = len(query_bigrams & chunk_bigrams)
                word_score = overlap / max(len(query_bigrams), 1)
            else:
                word_score = 0.0

            combined = sim * _SIM_WEIGHT + word_score * _WORD_WEIGHT
            if combined > _REFERENCE_THRESHOLD:
                scored.append((combined, chunk))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [c for _, c in scored[:top_k]]

    @staticmethod
    def _bigrams(text: str) -> set[str]:
        """把文本转成 2-gram 集合。短于 2 字的文本返回单字集合。"""
        if len(text) < 2:
            return set(text)
        return {text[i:i + 2] for i in range(len(text) - 1)}

    def _refresh_chunks(self) -> None:
        """检查 reference.txt 是否变化，变了就重新 chunk。"""
        if self._style_dir is None:
            return

        ref_path = self._style_dir / "reference.txt"

        if not ref_path.exists():
            # 文件不存在：如果之前有 chunks 就清掉（可能被用户删了）
            if self._chunk_signature is not None:
                self._chunks = []
                self._chunk_signature = None
                self._chunk_failed_signature = None
                logger.info("参考语料文件已删除: %s", ref_path)
            return

        try:
            stat = ref_path.stat()
        except OSError:
            return

        sig = (stat.st_mtime, stat.st_size)

        if sig == self._chunk_signature:
            return  # 没变
        if sig == self._chunk_failed_signature:
            return  # 还是同一个坏文件，跳过

        # 需要重新 chunk
        try:
            text = ref_path.read_text(encoding="utf-8").strip()
            if not text:
                # 空文件，静默跳过
                self._chunks = []
                self._chunk_signature = sig
                self._chunk_failed_signature = None
                return

            if len(text) < 100:
                logger.warning("参考语料较短（%d 字），建议 500+ 字以获得更好效果", len(text))

            chunks = self._chunk_reference(text)
        except OSError as exc:
            logger.warning("读取参考语料失败 %s: %s", ref_path, exc)
            self._chunk_failed_signature = sig
            return

        self._chunks = chunks
        self._chunk_signature = sig
        self._chunk_failed_signature = None
        logger.info(
            "参考语料加载成功: reference.txt (%d 字, %d 个 chunk)",
            len(text), len(chunks),
        )

    @staticmethod
    def _chunk_reference(text: str) -> list[str]:
        """把参考语料按段落切分成 chunks。

        规则：
        - 空行（\\n\\n）分段
        - 单段 > 300 字时，按句号/感叹号/问号/换行再拆
        - 每段保留 100-300 字，太短的会和下一段合并
        """
        raw_paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

        chunks: list[str] = []
        for para in raw_paragraphs:
            if len(para) <= 300:
                chunks.append(para)
            else:
                # 长段落按句子边界拆
                sentences = re.split(r'[。！？!?\n]', para)
                sentences = [s.strip() for s in sentences if s.strip()]
                # 合并短句到 100-300 字的 chunk
                buf = ""
                for s in sentences:
                    if len(buf) + len(s) <= 300:
                        buf += (s if not buf else "。" + s)
                    else:
                        if buf:
                            chunks.append(buf)
                        buf = s
                if buf:
                    chunks.append(buf)

        return chunks
