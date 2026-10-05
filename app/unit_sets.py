"""已发布的自定义单位定义集（custom unit sets）及其活动别名。

通过 ``POST /custom-unit-sets`` 把一组（1～6 项）已经按现有规则校验并展开
通过的自定义单位定义**发布**为可复用的定义集：服务生成不可猜测的标识，
定义内容落盘保存且**不可修改**；后续 ``/convert``、``/evaluate``、``/solve``
可凭标识选用该组定义。

在此之上，``POST /custom-unit-sets`` 之外还提供**活动别名**（alias）：
维护者把别名绑定到某个已发布定义集，调用方在三个换算接口中凭
``custom_unit_set_alias`` 沿用同一名称，维护者随后可把别名切换到新定义集。
绑定采用比较并交换（compare-and-swap）语义：提交 ``expected_id``，仅当
当前指向与其一致时才生效（初建为 ``null``），否则返回 409；目标定义集
不存在返回 422；两种失败都不会改写原指向。

设计要点：

- 发布前先用 :class:`~app.custom_units.UnitScope` 完整校验并展开全部条目
  （与请求内 ``custom_units`` 完全相同的规则），任一条目非法则不写入任何文件；
- 存储为数据目录下的单个 JSON 文件，写入采用“临时文件 + 原子替换”，
  容器重建/进程重启后重新加载，已发布内容与别名绑定都保持可用；
- 已发布内容不可变：只提供发布与读取，没有更新/删除接口；
- 别名切换与落盘共同生效：先按“新指向”写盘成功后才更新进程内指向，
  写盘失败时进程内指向保持不变；
- 旧版本存储文件缺少 ``aliases`` 字段时按空别名表加载，原定义集不受影响；
- 并发安全：所有读写由同一把可重入锁串行化；请求级换算仍构造独立的
  :class:`~app.custom_units.UnitScope`，互不共享、不修改全局单位表；
  换算请求在开始时解析一次别名，单次请求只会完整使用切换前或切换后的
  某一版定义。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
from fractions import Fraction

from .config import DATA_DIR
from .custom_units import CustomUnitSpec, UnitScope
from .errors import AliasConflictError, ConversionError
from .schemas import ALIAS_NAME_PATTERN, CustomUnitItem

#: 持久化文件名（位于 :data:`~app.config.DATA_DIR` 下）。
STORE_FILENAME = "custom_unit_sets.json"

#: 标识前缀（custom unit set）+ 随机字节数。
_ID_PREFIX = "cus_"
_ID_BYTES = 16

#: 存储文件格式版本。
_STORE_VERSION = 1

#: 别名命名校验（与 schema 层共用同一规则）。
_ALIAS_RE = re.compile(ALIAS_NAME_PATTERN)


def invalid_request(message: str) -> ConversionError:
    """与请求体相关的非法情况，统一归入 ``invalid_request``（HTTP 422）。"""

    return ConversionError("invalid_request", message)


def unit_set_not_found(set_id: str) -> ConversionError:
    """引用的定义集标识不存在（HTTP 422）。"""

    return ConversionError(
        "invalid_request", f"custom_unit_set_id 引用的定义集不存在: {set_id!r}"
    )


def alias_not_found(alias: str) -> ConversionError:
    """引用的活动别名不存在（HTTP 422）。"""

    return ConversionError(
        "invalid_request", f"custom_unit_set_alias 引用的别名不存在: {alias!r}"
    )


def alias_conflict(
    alias: str, current_id: str | None, expected_id: str | None
) -> AliasConflictError:
    """别名当前指向与 ``expected_id`` 不一致（HTTP 409）。"""

    return AliasConflictError(
        "alias_conflict",
        f"别名 {alias!r} 的当前指向为 "
        f"{current_id!r}，与提交的 expected_id {expected_id!r} 不一致；"
        "绑定未生效，请查询最新指向后重试",
        current_id,
    )


class UnitSetStore:
    """已发布定义集与活动别名的持久化存储（线程安全、原子落盘）。"""

    def __init__(self, data_dir: str = DATA_DIR) -> None:
        self._data_dir = data_dir
        self._path = os.path.join(data_dir, STORE_FILENAME)
        self._lock = threading.RLock()
        #: 标识 -> 已校验的定义项（按声明顺序）
        self._sets: dict[str, tuple[CustomUnitItem, ...]] = {}
        #: 活动别名 -> 定义集标识
        self._aliases: dict[str, str] = {}
        self._loaded = False

    @property
    def path(self) -> str:
        """持久化文件的完整路径。"""

        return self._path

    def load(self) -> None:
        """从数据目录加载已发布的定义集与别名；文件不存在时以空存储启动。

        旧版本文件缺少 ``aliases`` 字段时按空别名表加载，原定义集不受影响。
        文件存在但无法解析（或别名指向不存在的定义集）时抛出异常使启动失败
        （宁可不可用也不静默丢数据）。
        """

        with self._lock:
            if not os.path.exists(self._path):
                self._sets = {}
                self._aliases = {}
                self._loaded = True
                return
            with open(self._path, encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict) or raw.get("version") != _STORE_VERSION:
                raise ValueError(f"自定义单位定义集存储文件版本不被支持: {self._path}")
            sets: dict[str, tuple[CustomUnitItem, ...]] = {}
            for set_id, items in raw.get("sets", {}).items():
                sets[set_id] = tuple(
                    CustomUnitItem.model_validate(item) for item in items
                )
            aliases: dict[str, str] = {}
            for alias, set_id in raw.get("aliases", {}).items():
                if set_id not in sets:
                    raise ValueError(
                        f"自定义单位定义集存储文件损坏：别名 {alias!r} 指向不存在的"
                        f"定义集 {set_id!r}（{self._path}）"
                    )
                aliases[alias] = set_id
            self._sets = sets
            self._aliases = aliases
            self._loaded = True

    def publish(self, items: list[CustomUnitItem]) -> str:
        """先按现有规则校验并展开全部条目，通过后发布并返回服务生成的标识。

        任一条目非法（重名/冲突/循环/未知引用/禁引温标/非法比例等）都会
        抛出 :class:`~app.errors.ConversionError`，此时不写入、不留任何
        可引用的定义集。
        """

        self._ensure_loaded()
        specs = [
            CustomUnitSpec(
                symbol=item.symbol,
                factor=Fraction(item.factor),
                base_unit=item.base_unit,
            )
            for item in items
        ]
        # 构造期间立即解析全部声明；非法即抛 422，发生在任何写入之前
        UnitScope(specs)

        with self._lock:
            while True:
                set_id = _ID_PREFIX + secrets.token_hex(_ID_BYTES)
                if set_id not in self._sets:
                    break
            self._sets[set_id] = tuple(items)
            self._persist_locked()
        return set_id

    def get(self, set_id: str) -> list[CustomUnitItem]:
        """取已发布的定义项；不存在时抛出 :class:`ConversionError`（422）。"""

        self._ensure_loaded()
        with self._lock:
            items = self._sets.get(set_id)
        if items is None:
            raise unit_set_not_found(set_id)
        return list(items)

    def build_scope(self, set_id: str) -> UnitScope:
        """按已发布的定义集构造一次独立的请求级解析上下文。"""

        items = self.get(set_id)
        specs = [
            CustomUnitSpec(
                symbol=item.symbol,
                factor=Fraction(item.factor),
                base_unit=item.base_unit,
            )
            for item in items
        ]
        return UnitScope(specs)

    # -- 活动别名 -----------------------------------------------------------

    def resolve_alias(self, alias: str) -> str:
        """取别名当前指向的定义集标识；不存在时抛出 422。

        换算请求在开始时调用一次本方法取得标识，之后整次请求都使用该标识
        对应的（不可变）定义——与并发的别名切换互不影响。
        """

        self._ensure_loaded()
        with self._lock:
            set_id = self._aliases.get(alias)
        if set_id is None:
            raise alias_not_found(alias)
        return set_id

    def bind_alias(
        self, alias: str, set_id: str, expected_id: str | None
    ) -> None:
        """把 ``alias`` 绑定到 ``set_id``（比较并交换语义）。

        - 目标定义集不存在：抛出 422，原指向不变；
        - 当前指向与 ``expected_id`` 不一致（初建须传 ``None``）：抛出
          :class:`~app.errors.AliasConflictError`（409），原指向不变；
        - 校验通过后**先按新指向落盘**，写盘成功才更新进程内指向；
          写盘失败时异常向上传播，进程内指向保持不变。
        """

        if not _ALIAS_RE.match(alias):
            raise invalid_request(
                f"别名 {alias!r} 不合法：须以字母开头，"
                "仅含字母、数字、下划线、连字符（长度不超过 64）"
            )
        self._ensure_loaded()
        with self._lock:
            if set_id not in self._sets:
                raise invalid_request(f"绑定的目标定义集不存在: {set_id!r}")
            current = self._aliases.get(alias)
            if current != expected_id:
                raise alias_conflict(alias, current, expected_id)
            new_aliases = {**self._aliases, alias: set_id}
            # 切换与落盘共同生效：写盘失败时抛出异常，进程内指向不变
            self._persist_locked(aliases=new_aliases)
            self._aliases = new_aliases

    def count(self) -> int:
        """已发布的定义集数量（主要供测试与运维观察）。"""

        self._ensure_loaded()
        with self._lock:
            return len(self._sets)

    # -- 内部 -------------------------------------------------------------

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def _persist_locked(self, aliases: dict[str, str] | None = None) -> None:
        """把当前定义集与别名全量写入临时文件并原子替换（调用方须持锁）。

        ``aliases`` 缺省时落盘当前进程内别名表；绑定别名时传入“新指向”的
        别名表，使落盘先于进程内指向更新——写盘失败即抛出异常，调用方
        不会提交进程内变更。
        """

        os.makedirs(self._data_dir, exist_ok=True)
        payload = {
            "version": _STORE_VERSION,
            "sets": {
                set_id: [
                    {
                        "symbol": item.symbol,
                        "factor": str(item.factor),
                        "base_unit": item.base_unit,
                    }
                    for item in items
                ]
                for set_id, items in self._sets.items()
            },
            "aliases": aliases if aliases is not None else self._aliases,
        }
        tmp_path = f"{self._path}.tmp.{os.getpid()}.{secrets.token_hex(4)}"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, self._path)


#: 进程内共享的存储单例；由应用启动时初始化（也可在测试中重新初始化）。
_store: UnitSetStore | None = None
_store_lock = threading.RLock()


def init_store(data_dir: str = DATA_DIR) -> UnitSetStore:
    """（重新）初始化进程级存储单例并加载磁盘上的已发布定义集。"""

    global _store
    with _store_lock:
        store = UnitSetStore(data_dir)
        store.load()
        _store = store
    return store


def get_store() -> UnitSetStore:
    """获取进程级存储单例；尚未初始化时按默认数据目录初始化。"""

    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                init_store()
    assert _store is not None
    return _store
