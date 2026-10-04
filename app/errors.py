"""领域错误定义。

所有领域错误最终都以 HTTP 422 返回，响应体为::

    {"detail": {"error": "<错误码>", "message": "<人类可读信息>"}}
"""

from __future__ import annotations


class ConversionError(Exception):
    """换算过程中的领域错误（一律映射为 HTTP 422）。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def invalid_syntax(message: str) -> ConversionError:
    return ConversionError("invalid_syntax", message)


def unknown_symbol(symbol: str) -> ConversionError:
    return ConversionError("unknown_symbol", f"未知单位符号: {symbol!r}")


def unknown_variable(name: str) -> ConversionError:
    return ConversionError("unknown_variable", f"公式引用了未提供的变量: {name!r}")


def division_by_zero() -> ConversionError:
    return ConversionError("division_by_zero", "除数为零")


def invalid_power(message: str) -> ConversionError:
    return ConversionError("invalid_power", message)


def dimension_mismatch(src: str, dst: str) -> ConversionError:
    return ConversionError(
        "dimension_mismatch",
        f"量纲不兼容: 源量纲为 {src!r}, 目标量纲为 {dst!r}",
    )


def affine_mixing(message: str) -> ConversionError:
    return ConversionError("affine_mixing", message)


def precision_overflow(message: str) -> ConversionError:
    return ConversionError("precision_overflow", message)


def nonlinear_equation(message: str) -> ConversionError:
    return ConversionError("nonlinear_equation", message)


def no_unique_solution(message: str) -> ConversionError:
    return ConversionError("no_unique_solution", message)
