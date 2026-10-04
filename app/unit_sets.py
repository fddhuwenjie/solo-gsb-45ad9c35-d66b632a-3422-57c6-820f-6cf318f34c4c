"""已发布的自定义单位定义集（custom unit sets）。

通过 ``POST /custom-unit-sets`` 把一组（1～6 项）已经按现有规则校验并展开
通过的自定义单位定义**发布**为可复用的定义集：服务生成不可猜测的标识，
定义内容落盘保存且**不可修改**；后续 ``/convert``、``/evaluate``、``/solve``
可凭标识选用该组定义。

设计要点：

- 发布前先用 :class:`~app.custom_units.UnitScope` 完整校验并展开全部条目
  （与请求内 ``custom_units`` 完全相同的规则），任一条目非法则不写入任何文件；
- 存储为数据目录下的单个 JSON 文件，写入采用“临时文件 + 原子替换”，
  容器重建/进程重启后重新加载，已发布内容保持可用；
- 已发布内容不可变：只提供发布与读取，没有更新/删除接口；
- 并发安全：所有读写由同一把可重入锁串行化；请求级换算仍构造独立的
  :class:`~app.custom_units.UnitScope`，互不共享、不修改全局单位表。
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from fractions import Fraction

from .config import DATA_DIR
from .custom_units import CustomUnitSpec, UnitScope
from .errors import ConversionError
from .schemas import CustomUnitItem

#: 持久化文件名（位于 :data:`~app.config.DATA_DIR` 下）。
STORE_FILENAME = "custom_unit_sets.json"

#: 标识前缀（custom unit set）+ 随机字节数。
_ID_PREFIX = "cus_"
_ID_BYTES = 16

#: 存储文件格式版本。
_STORE_VERSION = 1


def invalid_request(message: str) -> ConversionError:
    """与请求体相关的非法情况，统一归入 ``invalid_request``（HTTP 422）。"""

    return ConversionError("invalid_request", message)


def unit_set_not_found(set_id: str) -> ConversionError:
    """引用的定义集标识不存在（HTTP 422）。"""

    return ConversionError(
        "invalid_request", f"custom_unit_set_id 引用的定义集不存在: {set_id!r}"
    )


class UnitSetStore:
    """已发布定义集的持久化存储（线程安全、原子落盘）。"""

    def __init__(self, data_dir: str = DATA_DIR) -> None:
        self._data_dir = data_dir
        self._path = os.path.join(data_dir, STORE_FILENAME)
        self._lock = threading.RLock()
        #: 标识 -> 已校验的定义项（按声明顺序）
        self._sets: dict[str, tuple[CustomUnitItem, ...]] = {}
        self._loaded = False

    @property
    def path(self) -> str:
        """持久化文件的完整路径。"""

        return self._path

    def load(self) -> None:
        """从数据目录加载已发布的定义集；文件不存在时以空存储启动。

        文件存在但无法解析时抛出异常使启动失败（宁可不可用也不静默丢数据）。
        """

        with self._lock:
            if not os.path.exists(self._path):
                self._sets = {}
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
            self._sets = sets
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

    def count(self) -> int:
        """已发布的定义集数量（主要供测试与运维观察）。"""

        self._ensure_loaded()
        with self._lock:
            return len(self._sets)

    # -- 内部 -------------------------------------------------------------

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def _persist_locked(self) -> None:
        """把当前定义集全量写入临时文件并原子替换（调用方须持锁）。"""

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
