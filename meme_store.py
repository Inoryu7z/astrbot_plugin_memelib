"""梗库的解析、渲染与加载。

规范格式为行文本，每行一条：

    梗名（变体1/变体2）｜含义：一句话说清含义｜用法：情境、情绪与例句

- 空行与以 # 开头的注释行忽略
- 名称后的变体组可省略；分隔符支持全角「｜」与半角「|」
- 自定义梗库除行文本外，也接受 JSON 数组（去除 ``` 代码围栏后，
  首字符为 "[" 即按 JSON 解析），字段为 name / aliases / meaning / usage
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from astrbot.api import logger

# 形如「1. 」「12、 」的行首序号
_LEADING_INDEX = re.compile(r"^\s*\d+\s*[.、:：]\s*")
# 名称尾部的变体组：梗名（变体1/变体2）
_ALIAS_GROUP = re.compile(r"^([^（(]+?)\s*[（(]\s*(.*?)\s*[）)]\s*$")
# 字段标签：含义：/用法:
_FIELD_LABEL = re.compile(r"^(含义|用法)\s*[：:]\s*")
_FENCE_LINE = re.compile(r"^```[A-Za-z0-9_-]*\s*$")
_ALIAS_SPLIT = re.compile(r"[/、，,]")


@dataclass(frozen=True)
class Meme:
    name: str
    aliases: tuple[str, ...]
    meaning: str
    usage: str


def _strip_fence(text: str) -> str:
    """去掉可能存在的 ```json ... ``` 包裹。"""
    lines = text.splitlines()
    if lines and _FENCE_LINE.match(lines[0].strip()):
        lines = lines[1:]
    if lines and _FENCE_LINE.match(lines[-1].strip()):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _parse_json_lib(raw: str) -> list[Meme]:
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("JSON 梗库顶层必须是数组")
    memes: list[Meme] = []
    for idx, item in enumerate(data, 1):
        if not isinstance(item, dict):
            raise ValueError(f"第 {idx} 条不是 JSON 对象")
        name = str(item.get("name", "")).strip()
        if not name:
            raise ValueError(f"第 {idx} 条缺少 name")
        aliases_raw = item.get("aliases") or []
        if isinstance(aliases_raw, str):
            aliases_raw = [aliases_raw]
        aliases = tuple(
            str(a).strip() for a in aliases_raw if str(a).strip()
        )
        meaning = str(item.get("meaning", "")).strip()
        # usage 为标准字段，example 仅作容错别名
        usage = str(item.get("usage") or item.get("example") or "").strip()
        memes.append(Meme(name, aliases, meaning, usage))
    return memes


def _parse_line(line: str) -> Meme | None:
    """解析单行；空行/注释行返回 None。"""
    text = line.strip()
    if not text or text.startswith("#"):
        return None

    segments = re.split(r"[｜|]", text)
    head = _LEADING_INDEX.sub("", segments[0].strip())
    aliases: tuple[str, ...] = ()
    match = _ALIAS_GROUP.match(head)
    if match:
        head = match.group(1).strip()
        aliases = tuple(
            part.strip()
            for part in _ALIAS_SPLIT.split(match.group(2))
            if part.strip()
        )

    fields: dict[str, str] = {}
    for segment in segments[1:]:
        label_match = _FIELD_LABEL.match(segment.strip())
        if label_match:
            fields[label_match.group(1)] = segment.strip()[
                label_match.end():
            ].strip()

    return Meme(
        name=head,
        aliases=aliases,
        meaning=fields.get("含义", ""),
        usage=fields.get("用法", ""),
    )


def parse_lib(raw: str, *, strict: bool = True) -> list[Meme]:
    """解析梗库文本。行文本或 JSON 数组均可。

    strict=True（内置库用）：任何一条缺名称或缺含义都抛 ValueError，避免
    半成品梗库静默注入。
    strict=False（自定义库用）：跳过格式不合法的行并记日志，其余照常生效；
    一条都没解析出来时才抛错，由调用方回退内置库。
    """
    text = _strip_fence(raw or "")
    if not text:
        raise ValueError("梗库内容为空")

    if text[0] == "[":
        memes = _parse_json_lib(text)
        if not memes:
            raise ValueError("JSON 数组为空")
    else:
        memes = []
        bad_lines: list[int] = []
        for lineno, line in enumerate(raw.splitlines(), 1):
            meme = _parse_line(line)
            if meme is None:
                continue
            if not meme.name or not meme.meaning:
                bad_lines.append(lineno)
                continue
            memes.append(meme)
        if bad_lines:
            preview = "、".join(str(n) for n in bad_lines[:5])
            message = (
                f"第 {preview} 行缺少梗名或「含义」字段，规范格式："
                "梗名｜含义：…｜用法：…"
            )
            if strict:
                raise ValueError(message)
            logger.warning(f"潮流梗库：已跳过 {message}")
        if not memes:
            raise ValueError("没有解析出任何有效条目")

    return memes


def merge_libs(base: list[Meme], extra: list[Meme]) -> list[Meme]:
    """把 extra 原样拼接到 base 之后，不做去重。

    追加模式的语义就是追加：内置库在前，用户条目在后，同名条目不替换、
    不合并，由注入顺序靠后的那一条在后文说明中覆盖。
    """
    return [*base, *extra]


def render_memes(memes: list[Meme]) -> str:
    """渲染为注入用的纯文本，每条一行并重新编号。"""
    lines: list[str] = []
    for index, meme in enumerate(memes, 1):
        head = f"{index}. {meme.name}"
        if meme.aliases:
            head += "（" + "/".join(meme.aliases) + "）"
        parts = [head]
        if meme.meaning:
            parts.append("含义：" + meme.meaning)
        if meme.usage:
            parts.append("用法：" + meme.usage)
        lines.append("｜".join(parts))
    return "\n".join(lines)


class BuiltinMemeStore:
    """内置 memes.txt：进程内只加载一次（插件热重载会产生新实例）。"""

    def __init__(self, path: str):
        self._path = path
        self._memes: list[Meme] = []
        self._rendered = ""
        self._count = 0
        self._load()

    def _load(self) -> None:
        try:
            # utf-8-sig 顺手吃掉可能的 BOM
            with open(self._path, "r", encoding="utf-8-sig") as f:
                raw = f.read()
        except FileNotFoundError:
            logger.warning("潮流梗库：内置 memes.txt 不存在，不注入梗库")
            return
        except OSError:
            logger.exception("潮流梗库：读取内置 memes.txt 失败，不注入梗库")
            return
        try:
            memes = parse_lib(raw)
        except ValueError:
            logger.exception("潮流梗库：内置 memes.txt 格式错误，不注入梗库")
            return
        self._memes = memes
        self._count = len(memes)
        self._rendered = render_memes(memes)
        logger.info(f"潮流梗库：内置梗库已加载，共 {self._count} 条")

    @property
    def memes(self) -> list[Meme]:
        return self._memes

    @property
    def rendered(self) -> str:
        return self._rendered
