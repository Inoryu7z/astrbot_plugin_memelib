import hashlib
import os
import re

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.provider.entities import ProviderRequest

from .meme_store import BuiltinMemeStore, parse_lib, render_memes

BUILTIN_LIB_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "memes.txt"
)

# 注入块用成对标签包裹，便于在 system_prompt 被复用时幂等清理
INJECTION_HEADER = "<MemeLib>"
INJECTION_FOOTER = "</MemeLib>"
_INJECTION_PATTERN = re.compile(
    re.escape(INJECTION_HEADER) + r".*?" + re.escape(INJECTION_FOOTER),
    flags=re.DOTALL,
)

LEVEL_LITE = "初级（酌情使用）"
LEVEL_FULL = "完整注入"

# 两档引导语均为用户定稿：共同把梗库定位为听懂材料并要求保持原人设；
# 初级档额外限制主动使用的频率与动机
DEFAULT_LITE_INTRO = (
    "以下梗库用于让你听懂对话中出现的流行梗。"
    "回复时保持你原有的说话方式与人设，"
    "只在语境自然契合时酌情使用，禁止滥用，禁止为了用梗而强行套用。"
)
DEFAULT_FULL_INTRO = (
    "以下梗库用于让你听懂对话中出现的流行梗。"
    "回复时保持你原有的说话方式与人设。"
)


def _remove_meme_injection(system_prompt: str | None) -> tuple[str, bool]:
    """移除已有的梗库注入块；返回（清理后的文本, 是否发生了清理）。"""
    if not system_prompt:
        return system_prompt or "", False
    cleaned = _INJECTION_PATTERN.sub("", system_prompt)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, cleaned != system_prompt


class MemePromptLib(Star):
    """潮流梗库：每次 LLM 请求前把精选流行梗全量注入系统提示词。

    - 初级档：梗库内容相同，引导语要求保持人设、酌情使用、禁止滥用。
    - 完整档：要求听懂并保持人设，不限制主动使用。
    注入方式与 dayflow 插件一致：成对标签包裹，每次请求先清除旧块再
    追加新块，保证 system_prompt 被跨请求复用时仍然幂等。
    内置 memes.txt 只加载一次；自定义梗库按内容哈希缓存渲染结果，
    解析失败时回退内置库。
    """

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        self._builtin = BuiltinMemeStore(BUILTIN_LIB_PATH)
        # (内容哈希, 渲染结果)；渲染失败时结果缓存为 None，避免刷日志
        self._custom_cache: tuple[str, str | None] | None = None

    def _render_custom(self, raw: str) -> str | None:
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()
        if self._custom_cache and self._custom_cache[0] == digest:
            return self._custom_cache[1]

        try:
            memes = parse_lib(raw)
        except ValueError as e:
            logger.warning(f"潮流梗库：自定义梗库解析失败（{e}），回退内置库")
            self._custom_cache = (digest, None)
            return None

        rendered = render_memes(memes)
        self._custom_cache = (digest, rendered)
        logger.info(f"潮流梗库：已启用自定义梗库，共 {len(memes)} 条")
        return rendered

    def _get_lib_body(self) -> str:
        raw = str(self.config.get("custom_meme_lib", "") or "").strip()
        if raw:
            body = self._render_custom(raw)
            if body:
                return body
        return self._builtin.rendered

    def _get_intro(self) -> str:
        if self.config.get("injection_level", LEVEL_LITE) == LEVEL_FULL:
            return (
                str(self.config.get("full_intro", "") or "").strip()
                or DEFAULT_FULL_INTRO
            )
        return (
            str(self.config.get("lite_intro", "") or "").strip()
            or DEFAULT_LITE_INTRO
        )

    @filter.on_llm_request()
    async def inject_meme_lib(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        try:
            if getattr(req, "system_prompt", None) is None:
                req.system_prompt = ""

            # 先清除上次注入，避免 system_prompt 被跨请求复用导致堆叠/残留
            req.system_prompt, removed = _remove_meme_injection(
                req.system_prompt
            )
            if removed:
                logger.debug("潮流梗库：已清理上次注入块")

            body = self._get_lib_body()
            if not body:
                return

            block = (
                f"{INJECTION_HEADER}\n{self._get_intro()}\n{body}\n"
                f"{INJECTION_FOOTER}"
            )
            req.system_prompt = (
                f"{req.system_prompt}\n\n{block}" if req.system_prompt else block
            )
        except Exception:
            logger.exception("潮流梗库：注入过程发生异常，已跳过")
