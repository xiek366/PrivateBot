"""角色状态系统（Stage 6）。

程序负责记住和计算，LLM 负责理解和表达。
不让 LLM 直接修改数值——只返回 event 名，Python 查表计算。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------- 防暴走常量 ----------
MAX_DELTA_PER_FIELD = 5.0  # 单次事件单字段最大变化幅度
# ---------- 情绪衰减常量 ----------
BASE_VALENCE = 20.0       # 平静时的 valence 基线（微正）
BASE_AROUSAL = 15.0       # 平静时的 arousal 基线（低激动）
DECAY_RATE = 0.1          # 每步骤衰减系数
TIME_SCALE_MINUTES = 30   # 30 分钟衰减一次完整步骤
MIN_DECAY_INTERVAL_MIN = 5  # 不足 5 分钟不衰减

# ---------- 关系事件表 ----------
# key = 事件名，value = 各字段变化量（affection/trust/familiarity）
RELATIONSHIP_EVENTS: dict[str, dict[str, float]] = {
    "user_greeted":        {"affection": 0.5, "familiarity": 0.5},
    "user_chat_normally": {"affection": 0.3, "familiarity": 0.3},
    "user_shared_life":    {"affection": 1.0, "familiarity": 0.5},
    "user_cares_about_her": {"affection": 2.0, "trust": 1.5},
    "user_praises_her":    {"affection": 1.0, "trust": 0.5},
    "user_says_he_likes_her": {"affection": 2.0, "trust": 1.0},
    "user_comforted_her":  {"affection": 2.5, "trust": 2.0},
    "user_listens_patiently": {"trust": 2.0, "familiarity": 1.0},
    "user_shares_secret":  {"affection": 1.5, "trust": 3.0},
    "user_completed_goal": {"affection": 1.5, "trust": 1.0},
    "user_needs_comfort":  {"affection": 0.5},
    # 负向
    "user_misled_her":     {"affection": -3.0, "trust": -6.0},
    "user_hurt_her":       {"affection": -5.0, "trust": -10.0},
    "user_apologizes":     {"trust": 3.0, "affection": 1.0},
}

# ---------- 情绪事件表 ----------
# key = 事件名，value = valence/arousal 变化量
EMOTION_EVENTS: dict[str, dict[str, float]] = {
    "user_greeted":        {"valence": 5,   "arousal": 3},
    "user_chat_normally":  {"valence": 2,   "arousal": 1},
    "user_shared_life":    {"valence": 8,   "arousal": 5},
    "user_cares_about_her": {"valence": 15,  "arousal": 12},
    "user_praises_her":    {"valence": 18,  "arousal": 25},
    "user_says_he_likes_her": {"valence": 20, "arousal": 35},
    "user_comforted_her":  {"valence": 25,  "arousal": 10},
    "user_listens_patiently": {"valence": 12, "arousal": 5},
    "user_shares_secret":  {"valence": 15,  "arousal": 8},
    "user_completed_goal": {"valence": 15,  "arousal": 10},
    "user_needs_comfort":  {"valence": -5,  "arousal": 8},
    "user_misled_her":     {"valence": -25, "arousal": 30},
    "user_hurt_her":       {"valence": -35, "arousal": 45},
    "user_apologizes":     {"valence": 10,  "arousal": -5},
}

# ---------- 事件匹配规则 ----------
# key = 事件名，value = 正则 pattern 列表（任一匹配即触发该事件）
# 多条事件可同时匹配 → 数值累加
# 覆盖原则：事件表里除兜底 user_chat_normally 外，每条事件都必须有 pattern 组
EVENT_PATTERNS: dict[str, list[str]] = {
    "user_greeted":           [r"早上好", r"早安", r"中午好", r"下午好", r"晚上好", r"晚安", r"你好", r"在吗", r"在么", r"嗨"],
    "user_says_he_likes_her": [r"我喜欢你", r"我爱你", r"喜欢你", r"爱你"],
    "user_cares_about_her":   [r"你.*怎么样", r"你.*还好吗", r"有没有吃饭", r"累不累", r"还好吗", r"在干嘛"],
    "user_praises_her":       [r"你真可爱", r"你好可爱", r"好漂亮", r"真棒", r"你好美", r"你真好看"],
    "user_comforted_her":     [r"别难过", r"别伤心", r"我在这", r"我陪着你", r"不要哭", r"没事的"],
    "user_needs_comfort":     [r"好累啊?", r"好难过", r"好累", r"被骂", r"搞砸了", r"失败了", r"好烦"],
    "user_shared_life":       [r"我今天", r"我昨天", r"我刚才", r"我刚刚", r"我跟你说", r"我跟你讲"],
    "user_listens_patiently": [r"你继续说", r"继续说", r"我在听", r"你讲吧", r"你说吧", r"然后呢"],
    "user_shares_secret":     [r"告诉你个?秘密", r"我只跟你说", r"别告诉别人", r"这是秘密", r"我从来没跟.*说过"],
    "user_apologizes":        [r"对不起", r"抱歉", r"我错了", r"原谅我", r"是我不好"],
    "user_completed_goal":    [r"通过了", r"过了", r"完成了", r"做到了", r"成功了", r"拿到了"],
    "user_hurt_her":          [r"滚[！！]?", r"闭嘴[！！]?", r"烦不烦", r"你算什么"],
    "user_misled_her":        [r"我骗了你", r"我刚才是骗你的", r"我撒谎了", r"我一直在骗你"],
}


@dataclass
class CharacterState:
    """每用户一份的角色状态。"""
    affection: float = 20.0
    trust: float = 15.0
    familiarity: float = 20.0
    valence: float = 15.0       # 情绪正负 -100 ~ +100
    arousal: float = 12.0       # 情绪激烈 0 ~ 100
    interaction_count: int = 0
    last_interaction: str = ""
    created_at: str = ""
    decay_at: str = ""

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> "CharacterState":
        """从 JSON dict 构造，缺失字段用默认值。"""
        defaults = cls()
        for field_name in cls.__dataclass_fields__:
            if field_name in data:
                setattr(defaults, field_name, data[field_name])
        return defaults


class CharacterStateManager:
    """角色状态管理器：加载/保存 + 事件匹配 + 数值计算 + 衰减。"""

    def __init__(self, state_dir: Optional[Path]) -> None:
        self._dir = state_dir
        self._cache: dict[int, CharacterState] = {}

    # ---------- 基础 IO ----------

    @property
    def enabled(self) -> bool:
        """状态系统是否启用（配置了 STATE_DIR 且目录存在或可创建）。"""
        return self._dir is not None

    def load(self, user_id: int) -> CharacterState:
        """加载用户状态。文件不存在则创建默认状态并保存。"""
        if user_id in self._cache:
            return self._cache[user_id]

        if self._dir is None:
            return self._default_state()

        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / f"{user_id}.json"

        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                state = CharacterState.from_json(data)
                self._cache[user_id] = state
                return state
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("读取状态文件失败 %s: %s，使用默认值", path, exc)

        # 文件不存在或损坏 → 创建默认
        state = self._default_state()
        state.created_at = _now_iso()
        state.decay_at = _now_iso()
        self._cache[user_id] = state
        self.save(user_id, state)
        logger.info("角色状态初始化 user_id=%d: affection=%.0f trust=%.0f",
                    user_id, state.affection, state.trust)
        return state

    def save(self, user_id: int, state: CharacterState) -> None:
        """保存用户状态到 JSON 文件。"""
        if self._dir is None:
            return
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            path = self._dir / f"{user_id}.json"
            path.write_text(json.dumps(state.to_json(), ensure_ascii=False, indent=2),
                            encoding="utf-8")
        except OSError as exc:
            logger.warning("保存状态文件失败 user_id=%d: %s", user_id, exc)

    def _default_state(self) -> CharacterState:
        """新建用户的默认状态。Level 1 刚认识。"""
        return CharacterState(
            affection=20.0,
            trust=15.0,
            familiarity=20.0,
            valence=15.0,
            arousal=12.0,
        )

    def mark_interaction(self, state: CharacterState) -> None:
        """记录一次交互：计数 +1，并写入交互时间（供主动消息判断"上次聊天多久前"）。"""
        state.interaction_count += 1
        state.last_interaction = _now_iso()

    # ---------- 事件匹配 ----------

    def match_events(self, user_text: str) -> list[str]:
        """从用户消息中匹配所有触发的事件。无匹配返回 ['user_chat_normally'] 兜底。"""
        matched: list[str] = []
        for event_key, patterns in EVENT_PATTERNS.items():
            for pat in patterns:
                if re.search(pat, user_text):
                    matched.append(event_key)
                    break  # 同一事件只触发一次

        # 兜底：什么都没匹配到 → 普通聊天
        if not matched:
            matched = ["user_chat_normally"]

        return matched

    # ---------- 数值应用（程序算，不让 LLM 改）----------

    def apply_events(self, state: CharacterState, event_keys: list[str]) -> None:
        """查表计算数值变化，带防暴走裁剪。"""
        # 合并所有事件的数值变化
        total_delta: dict[str, float] = {}
        for key in event_keys:
            rel_delta = RELATIONSHIP_EVENTS.get(key, {})
            emo_delta = EMOTION_EVENTS.get(key, {})
            for field, val in {**rel_delta, **emo_delta}.items():
                total_delta[field] = total_delta.get(field, 0.0) + val

        # 裁剪 + 边界保护
        for field, raw_delta in total_delta.items():
            # 防暴走：单次单字段 ±MAX_DELTA_PER_FIELD
            clipped = max(-MAX_DELTA_PER_FIELD, min(MAX_DELTA_PER_FIELD, raw_delta))

            if field in ("affection", "trust", "familiarity"):
                new_val = getattr(state, field) + clipped
                setattr(state, field, max(0.0, min(100.0, new_val)))
            elif field == "valence":
                new_val = state.valence + clipped
                state.valence = max(-100.0, min(100.0, new_val))
            elif field == "arousal":
                new_val = state.arousal + clipped
                state.arousal = max(0.0, min(100.0, new_val))

    # ---------- 情绪衰减 ----------

    def decay(self, state: CharacterState) -> None:
        """情绪逐渐回归平静基线。affection/trust/familiarity 不衰减。"""
        now = datetime.now()

        try:
            last_decay = datetime.fromisoformat(state.decay_at) if state.decay_at else now
        except ValueError:
            last_decay = now

        elapsed_minutes = (now - last_decay).total_seconds() / 60.0
        if elapsed_minutes < MIN_DECAY_INTERVAL_MIN:
            return  # 太频繁，跳过

        steps = int(elapsed_minutes / TIME_SCALE_MINUTES)
        if steps <= 0:
            return

        for _ in range(steps):
            state.valence += (BASE_VALENCE - state.valence) * DECAY_RATE
            state.arousal += (BASE_AROUSAL - state.arousal) * DECAY_RATE

        state.decay_at = _now_iso()

    # ---------- 关系等级映射 ----------

    @staticmethod
    def calc_relationship_level(affection: float) -> int:
        if affection >= 95:
            return 5  # 深度依赖
        if affection >= 80:
            return 4  # 恋人
        if affection >= 60:
            return 3  # 暧昧
        if affection >= 40:
            return 2  # 亲近
        if affection >= 20:
            return 1  # 熟悉
        return 0       # 刚认识

    LEVEL_NAMES = ["刚认识", "熟悉", "亲近", "暧昧", "恋人", "深度依赖"]

    # ---------- 状态 → Prompt 文本 ----------

    def state_to_prompt(self, state: CharacterState) -> str:
        """把数值状态转成自然语言给 LLM 看。"""
        level = self.calc_relationship_level(state.affection)
        level_name = self.LEVEL_NAMES[level]

        # 情绪标签（从 valence + arousal 推断）
        if state.valence > 30 and state.arousal > 20:
            mood = "开心"
        elif state.valence > 30 and state.arousal <= 20:
            mood = "平静但有暖意"
        elif state.valence > 0 and state.arousal > 25:
            mood = "害羞（情绪正向但激动）"
        elif state.valence < -20 and state.arousal > 20:
            mood = "生气或不安"
        elif state.valence < -10:
            mood = "低落"
        else:
            mood = "平静"

        return (
            f"[当前关系状态]\n"
            f"关系等级：{level}（{level_name}）\n"
            f"好感度：{state.affection:.0f}/100\n"
            f"信任度：{state.trust:.0f}/100\n"
            f"熟悉度：{state.familiarity:.0f}/100\n\n"
            f"[当前情绪]\n"
            f"情绪倾向：{mood}\n"
            f"情绪正负：{state.valence:.0f}（-100 极度负面 ←→ +100 极度正面）\n"
            f"情绪激烈：{state.arousal:.0f}/100"
        )


# ---------- 辅助函数 ----------

def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")
