"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

除了普通业务模块，这里还承载一份内部的「函数引用数据结果」表（udf_ref）：
它由自定义函数库的引用校验写入、被流处理算子发布读取，属于派生数据，
因此不出现在运营概览的业务模块清单里。

并发写入用一把可重入锁串行化；`transaction()` 在事务失败时把涉及的
表整体还原，保证函数字段和算子引用不会只改一半。
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from copy import deepcopy
from typing import Any, Iterator

from app.seed import SEED_ROWS

# 内部派生表：不参与运营概览统计，也不在健康检查的模块数里。
INTERNAL_MODULES = {"udf_ref"}


class TransactionAbort(Exception):
    """在事务里主动放弃：携带可读原因，由 `transaction()` 整组回退后交给调用方。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message



class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self._lock = threading.RLock()

    @property
    def lock(self) -> threading.RLock:
        """全局可重入写锁：停用函数与算子发布共用同一把，避免竞态交叉落库。"""
        return self._lock

    def module_names(self) -> list[str]:
        return sorted(name for name in self._tables if name not in INTERNAL_MODULES)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def find_by(self, module: str, field: str, value: Any) -> dict[str, Any] | None:
        """按业务编号（例如函数编号、记录编号）查找一条记录。"""
        for row in self.rows(module):
            if str(row.get(field, "")) == str(value):
                return row
        return None

    @contextmanager
    def transaction(self, *modules: str) -> Iterator["Store"]:
        """串行执行一组写操作；任一环节失败时整组回退。

        两种失败都会还原整表快照：
        - 抛出 `TransactionAbort`：业务原因拒绝，回退后把消息交给调用方；
        - 抛出其他异常：回退后继续向上抛，由接口层转成错误响应。

        锁是可重入的，因此嵌套调用（例如服务里再调用引用索引校验）
        不会互相死锁。回退采用整表快照还原，调用方拿到的旧 dict 引用
        会失效，失败后应重新 `find` 读取。
        """
        names = tuple(modules) if modules else tuple(self._tables)
        with self._lock:
            snapshot = {name: deepcopy(self.rows(name)) for name in names}
            try:
                yield self
            except BaseException:
                for name in names:
                    self._tables[name] = deepcopy(snapshot[name])
                raise

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
