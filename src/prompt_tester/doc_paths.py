"""工具用的路徑定位：在 JSON 化的文件（預設 / 情境）上讀、設值、新增、刪除。

路徑語法：以 `.` 分隔欄位，`[鍵]` 取清單元素。
- 一般清單用索引：`user_turns[0]`、`card.alternate_greetings[1]`、`history[2].content`
- 有主鍵的清單用主鍵：`blocks[<區塊 id 或 6 碼以上前綴>]`、`card.book.entries[<uid>]`
- 別名：`lore[12]` = `card.book.entries[12]`

修改只發生在 dict / list 上，呼叫端改完再用 pydantic 驗證整份文件；這裡負責「欄位存不存在、能不能改」。
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

_TOKEN = re.compile(r"([^.\[\]]+)|\[([^\]]*)\]|(\.)")


class PathError(ValueError):
    pass


@dataclass
class Spec:
    # 有主鍵的清單：欄位名稱路徑 → 主鍵欄位
    keyed: dict[tuple[str, ...], str] = field(default_factory=dict)
    # 可以用前綴比對的主鍵（區塊 id）
    prefix_keys: set[str] = field(default_factory=set)
    aliases: dict[str, str] = field(default_factory=dict)
    # 可以新增 / 刪除任意鍵的 dict（以欄位名稱路徑表示）
    free: set[tuple[str, ...]] = field(default_factory=set)
    # 不能修改的欄位（欄位名稱路徑前綴）
    blocked: set[tuple[str, ...]] = field(default_factory=set)


@dataclass
class Loc:
    """路徑解析結果：parent[key] 就是目標。names 是不含索引的欄位名稱路徑。"""

    parent: Any
    key: Any
    names: tuple[str, ...]
    exists: bool

    @property
    def value(self) -> Any:
        return self.parent[self.key] if self.exists else None


def parse(path: str, spec: Spec) -> list[tuple[str, str]]:
    """→ [("field", 名稱) | ("item", 鍵)]"""
    path = (path or "").strip()
    if not path:
        raise PathError("path 不能是空的")
    for alias, target in spec.aliases.items():
        if path == alias or path.startswith(alias + "[") or path.startswith(alias + "."):
            path = target + path[len(alias):]
            break
    out: list[tuple[str, str]] = []
    pos = 0
    for m in _TOKEN.finditer(path):
        if m.start() != pos:
            raise PathError(f"路徑格式錯誤：{path}")
        pos = m.end()
        if m.group(1) is not None:
            out.append(("field", m.group(1).strip()))
        elif m.group(2) is not None:
            out.append(("item", m.group(2).strip()))
    if pos != len(path) or not out or out[0][0] != "field":
        raise PathError(f"路徑格式錯誤：{path}")
    return out


def _item_index(lst: list, key: str, names: tuple[str, ...], spec: Spec, path: str) -> int:
    keyfield = spec.keyed.get(names)
    if keyfield is None:
        try:
            i = int(key)
        except ValueError:
            raise PathError(f"{'.'.join(names)} 要用數字索引（{path}）") from None
        if not 0 <= i < len(lst):
            raise PathError(f"{'.'.join(names)} 只有 {len(lst)} 項，索引 {i} 超出範圍")
        return i
    hits = [i for i, x in enumerate(lst) if str(x.get(keyfield)) == key]
    if not hits and keyfield in spec.prefix_keys and len(key) >= 6:
        hits = [i for i, x in enumerate(lst) if str(x.get(keyfield, "")).startswith(key)]
    if not hits:
        raise PathError(f"{'.'.join(names)} 裡找不到 {keyfield}={key}")
    if len(hits) > 1:
        raise PathError(f"{keyfield} 前綴 {key} 不唯一")
    return hits[0]


def locate(doc: dict, path: str, spec: Spec) -> Loc:
    tokens = parse(path, spec)
    cur: Any = doc
    names: tuple[str, ...] = ()
    for n, (kind, key) in enumerate(tokens):
        last = n == len(tokens) - 1
        if kind == "field":
            if not isinstance(cur, dict):
                raise PathError(f"{'.'.join(names) or '文件'} 不是物件，不能取 .{key}（{path}）")
            exists = key in cur
            if not exists and not (last and names in spec.free):
                known = "、".join(list(cur)[:30])
                raise PathError(f"沒有欄位 {'.'.join((*names, key))}；這裡的欄位：{known}")
            child_names = (*names, key)
            if last:
                return Loc(cur, key, child_names, exists)
            names = child_names
            cur = cur[key]
            if cur is None:
                raise PathError(f"{'.'.join(names)} 目前是空的（null）")
        else:
            if not isinstance(cur, list):
                raise PathError(f"{'.'.join(names)} 不是清單，不能用 [{key}]（{path}）")
            i = _item_index(cur, key, names, spec, path)
            if last:
                return Loc(cur, i, names, True)
            cur = cur[i]
    raise PathError(f"路徑格式錯誤：{path}")


def check_writable(loc: Loc, spec: Spec, extra: Callable[[tuple[str, ...]], str | None] | None = None) -> None:
    for b in spec.blocked:
        if loc.names[: len(b)] == b:
            raise PathError(f"{'.'.join(loc.names)} 不能修改")
    if extra and (msg := extra(loc.names)):
        raise PathError(msg)


def set_value(doc: dict, path: str, value: Any, spec: Spec, extra=None) -> Loc:
    loc = locate(doc, path, spec)
    check_writable(loc, spec, extra)
    loc.parent[loc.key] = value
    return loc


def insert(doc: dict, path: str, value: Any, spec: Spec, index: int | None = None, extra=None) -> tuple[Loc, int]:
    """path 指向清單：插入到 index（省略時加在最後）。"""
    loc = locate(doc, path, spec)
    check_writable(loc, spec, extra)
    lst = loc.value
    if not isinstance(lst, list):
        raise PathError(f"{path} 不是清單；add 只能用在清單上（改單一欄位請用 set）")
    pos = len(lst) if index is None else max(0, min(index, len(lst)))
    lst.insert(pos, value)
    return loc, pos


def remove(doc: dict, path: str, spec: Spec, extra=None) -> Any:
    loc = locate(doc, path, spec)
    check_writable(loc, spec, extra)
    if isinstance(loc.parent, list):
        return loc.parent.pop(loc.key)
    if loc.names[:-1] in spec.free and loc.exists:
        return loc.parent.pop(loc.key)
    raise PathError(f"{path} 不是清單元素，不能刪除（要清空請用 set）")
