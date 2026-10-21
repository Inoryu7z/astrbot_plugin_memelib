import hashlib
import os
import re

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.provider.entities import ProviderRequest

from .meme_store import BuiltinMemeStore, Meme, merge_libs, parse_lib, render_memes

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

MODE_OVERWRITE = "override"
MODE_APPEND = "append"

# 兼容早期本地试跑写进配置的中文取值
_LEGACY_MODE = {"覆写": MODE_OVERWRITE, "追加": MODE_APPEND}

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


def _strip_builtin_prefix(
    custom: list[Meme], builtin: list[Meme]
) -> list[Meme]:
    """追加模式：去掉自定义库开头照抄的内置库副本，只留后面的增量。

    配置面板在覆写模式下预填了整库，用户切到追加模式后往往直接在末尾
    续写，此时自定义库是「内置库 + 增量」，原样拼接会重复注入一遍。
    """
    size = len(builtin)
    if size and len(custom) > size and custom[:size] == builtin:
        return custom[size:]
    return custom


class MemePromptLib(Star):
    """潮流梗库：每次 LLM 请求前把精选流行梗全量注入系统提示词。

    - 初级档：梗库内容相同，引导语要求保持人设、酌情使用、禁止滥用。
    - 完整档：要求听懂并保持人设，不限制主动使用。
    - 自定义库两模式：override（整体替换内置库）与 append（内置库在前，
      自定义条目原样追加在后，不去重）。
    注入方式与 dayflow 插件一致：成对标签包裹，每次请求先清除旧块再
    追加新块，保证 system_prompt 被跨请求复用时仍然幂等。
    内置 memes.txt 只加载一次；自定义梗库按内容哈希缓存解析结果，
    解析失败时回退内置库。自定义库留空即完整使用内置库。
    """

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        self._builtin = BuiltinMemeStore(BUILTIN_LIB_PATH)
        # (内容哈希, 解析结果)；解析失败时结果缓存为 None，避免刷日志
        self._custom_cache: tuple[str, list[Meme] | None] | None = None
        self._sync_custom_lib()

    def _sync_custom_lib(self) -> None:
        """让配置面板里的自定义库与当前模式匹配。

        - 覆写模式且为空：填入内置库全文，打开面板即可看到完整初始库。
        - 追加模式：内容与内置库完全相同说明没有任何增量，清空它，
          否则整库副本会原样再拼一遍。
        """
        try:
            raw = str(self.config.get("custom_meme_lib", "") or "").strip()
            if self._get_mode() == MODE_APPEND:
                if not raw:
                    return
                custom = self._parse_custom(raw)
                if custom is None:
                    return
                stripped = _strip_builtin_prefix(custom, self._builtin.memes)
                if stripped == self._builtin.memes:
                    stripped = []
                if stripped != custom:
                    self.config["custom_meme_lib"] = (
                        render_memes(stripped) if stripped else ""
                    )
                    self._save_config()
                    logger.info(
                        "潮流梗库：追加模式下已去掉自定义库里的内置库副本，"
                        f"保留增量 {len(stripped)} 条"
                    )
                return

            if raw:
                return
            if not self._builtin.rendered:
                return
            self.config["custom_meme_lib"] = self._builtin.rendered
            self._save_config()
            logger.info(
                "潮流梗库：自定义梗库为空，已填入内置梗库"
                f"（{len(self._builtin.memes)} 条）"
            )
        except Exception:
            logger.exception("潮流梗库：同步自定义梗库失败，保持原样")

    def _save_config(self) -> None:
        save = getattr(self.config, "save_config", None)
        if callable(save):
            save()

    def _parse_custom(self, raw: str) -> list[Meme] | None:
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()
        if self._custom_cache and self._custom_cache[0] == digest:
            return self._custom_cache[1]

        try:
            memes = parse_lib(raw, strict=False)
        except ValueError as e:
            logger.warning(f"潮流梗库：自定义梗库解析失败（{e}），回退内置库")
            self._custom_cache = (digest, None)
            return None

        self._custom_cache = (digest, memes)
        logger.info(f"潮流梗库：已解析自定义梗库，共 {len(memes)} 条")
        return memes

    def _get_mode(self) -> str:
        mode = str(self.config.get("custom_mode", MODE_OVERWRITE) or "").strip()
        return _LEGACY_MODE.get(mode, mode)

    def _get_lib_body(self) -> str:
        raw = str(self.config.get("custom_meme_lib", "") or "").strip()
        custom = self._parse_custom(raw) if raw else None
        # 与内置库一致视为未做定制，避免追加模式下把内置库再拼一遍
        if custom == self._builtin.memes:
            custom = None

        if self._get_mode() == MODE_APPEND:
            if custom is not None:
                custom = _strip_builtin_prefix(custom, self._builtin.memes)
            memes = merge_libs(self._builtin.memes, custom or [])
        else:
            memes = custom if custom is not None else self._builtin.memes

        return render_memes(memes) if memes else ""

    def _get_intro(self) -> str:
        if self.config.get("injection_level", LEVEL_LITE) == LEVEL_FULL:
            return DEFAULT_FULL_INTRO
        return DEFAULT_LITE_INTRO

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
