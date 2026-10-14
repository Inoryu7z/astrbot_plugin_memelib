import json
import os

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star

BUILTIN_LIB_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "memes.json"
)

# 注入块的固定节标题，同时作为防重复注入的哨兵
SECTION_TITLE = "【流行梗库】"

LEVEL_LITE = "初级（酌情使用）"
LEVEL_FULL = "完整注入"

DEFAULT_LITE_INTRO = "以下网络流行梗供参考，请酌情使用。"
DEFAULT_FULL_INTRO = "梗库如下："


class MemePromptLib(Star):
    """潮流梗库：把精选网络流行梗注入系统提示词，补充 LLM 的世界知识。

    - lite 档：全量注入梗库，引导语含"酌情"，防止人设被压成梗小鬼。
    - full 档：全量注入梗库，引导语不设克制。
    两档注入的梗库内容相同，仅引导语不同。
    """

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}

    # ---------- 梗库加载 ----------

    def _load_builtin(self) -> list:
        try:
            with open(BUILTIN_LIB_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return []
        except Exception:
            logger.exception("潮流梗库：内置 memes.json 解析失败，本次不注入")
            return []
        if not isinstance(data, list):
            logger.warning("潮流梗库：内置 memes.json 顶层不是数组，本次不注入")
            return []
        return data

    @staticmethod
    def _parse_custom(raw) -> list | None:
        """解析配置面板里的自定义梗库。空配置返回 None（回退内置库）。"""
        if not raw or not str(raw).strip():
            return None
        text = str(raw).strip()
        # 容错：用户可能粘了 ```json ... ``` 包裹的内容
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        data = json.loads(text)
        if not isinstance(data, list):
            raise ValueError("自定义梗库顶层必须是 JSON 数组")
        return data

    # ---------- 渲染 ----------

    @staticmethod
    def _render(entries: list) -> str:
        lines = []
        index = 0
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name", "")).strip()
            if not name:
                continue
            meaning = str(entry.get("meaning", "")).strip()
            example = str(entry.get("example", "")).strip()

            aliases = entry.get("aliases") or []
            if isinstance(aliases, str):
                aliases = [aliases] if aliases.strip() else []
            aliases = [str(a).strip() for a in aliases if str(a).strip()]

            index += 1
            line = f"{index}. {name}"
            if aliases:
                line += "（又称：" + "、".join(aliases) + "）"
            if meaning:
                line += "：" + meaning
            if example:
                line += f" 例：{example}"
            lines.append(line)
        return "\n".join(lines)

    # ---------- 注入 ----------

    @filter.on_llm_request()
    async def inject_meme_lib(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        try:
            if SECTION_TITLE in (req.system_prompt or ""):
                return

            try:
                entries = self._parse_custom(
                    self.config.get("custom_meme_lib", "")
                )
            except Exception as e:
                logger.warning(
                    f"潮流梗库：自定义梗库解析失败（{e}），回退内置 memes.json"
                )
                entries = None

            if entries is None:
                entries = self._load_builtin()
            if not entries:
                return

            if self.config.get("injection_level", LEVEL_LITE) == LEVEL_FULL:
                intro = self.config.get("full_intro", "") or DEFAULT_FULL_INTRO
            else:
                intro = self.config.get("lite_intro", "") or DEFAULT_LITE_INTRO

            rendered = self._render(entries)
            if not rendered:
                return

            req.system_prompt = (
                (req.system_prompt or "")
                + f"\n\n{SECTION_TITLE}\n{intro}\n{rendered}"
            )
        except Exception:
            logger.exception("潮流梗库：注入过程发生异常，已跳过")
