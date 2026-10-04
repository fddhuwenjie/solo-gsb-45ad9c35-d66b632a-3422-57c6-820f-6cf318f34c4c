"""Pydantic 请求/响应模型。"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .config import MAX_PRECISION


def _coerce_value(v: Any) -> Any:
    """JSON -> Decimal 预处理：拒绝布尔，float 取最短十进制表示避免二进制污染。"""
    if isinstance(v, bool):
        raise ValueError("value 必须是十进制数")
    if isinstance(v, float):
        v = repr(v)
    return v


def _check_value(d: Decimal) -> Decimal:
    if not d.is_finite():
        raise ValueError("value 必须为有限十进制数")
    if len(d.as_tuple().digits) > MAX_PRECISION:
        raise ValueError(f"value 有效数字超过 {MAX_PRECISION} 位的精度上限")
    return d


class CustomUnitItem(BaseModel):
    """请求级自定义线性单位声明：``1 symbol = factor 个 base_unit``。"""

    symbol: str = Field(min_length=1, max_length=32, description="自定义单位符号")
    factor: Decimal = Field(
        gt=0, description="正十进制比例：1 symbol = factor 个 base_unit"
    )
    base_unit: str = Field(
        min_length=1,
        max_length=200,
        description="基准单位表达式（可引用本次声明的其他自定义单位；"
        "不得引用 degC、degF 或裸 K）",
    )

    model_config = ConfigDict(populate_by_name=True)

    @field_validator("factor", mode="before")
    @classmethod
    def _coerce_factor(cls, v: Any) -> Any:
        return _coerce_value(v)

    @field_validator("factor")
    @classmethod
    def _check_factor(cls, d: Decimal) -> Decimal:
        return _check_value(d)


#: 三个换算接口均可携带的请求级自定义单位声明（1～6 项）。
CustomUnits = list[CustomUnitItem]


class CustomUnitsSelectionModel(BaseModel):
    """三个换算接口共用的自定义单位来源选择。

    自定义单位可以来自请求内的 ``custom_units``（仅本次请求有效），也可以来自
    已发布的定义集标识 ``custom_unit_set_id``；二者互斥，同时出现整次返回 422。
    二者都缺省时三个接口保持原有行为。
    """

    model_config = ConfigDict(populate_by_name=True)

    custom_units: CustomUnits | None = Field(
        default=None,
        min_length=1,
        max_length=6,
        description="请求级自定义线性单位声明（1～6 项），仅在本次请求内有效；"
        "与 custom_unit_set_id 互斥",
    )
    custom_unit_set_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="已发布自定义单位定义集的服务端标识；与 custom_units 互斥，"
        "标识不存在时整次请求返回 422",
    )

    @model_validator(mode="after")
    def _check_custom_units_sources_exclusive(self) -> "CustomUnitsSelectionModel":
        if self.custom_units is not None and self.custom_unit_set_id is not None:
            raise ValueError(
                "custom_units 与 custom_unit_set_id 不能同时出现："
                "一次调用只能选用一种自定义单位来源"
            )
        return self


class ConvertRequest(CustomUnitsSelectionModel):
    """POST /convert 请求体。

    ``value`` 建议以 JSON 字符串传递以保证十进制精确性；
    JSON 数值也会被接受（按其最短十进制表示解释）。
    """

    value: Decimal = Field(description="十进制定值")
    from_unit: str = Field(alias="from", min_length=1, max_length=200, description="源单位表达式")
    to_unit: str = Field(alias="to", min_length=1, max_length=200, description="目标单位表达式")

    @field_validator("value", mode="before")
    @classmethod
    def _coerce_value(cls, v: Any) -> Any:
        return _coerce_value(v)

    @field_validator("value")
    @classmethod
    def _check_value(cls, d: Decimal) -> Decimal:
        return _check_value(d)


class Variable(BaseModel):
    """POST /evaluate 中的单个变量：带 ``value`` 与 ``unit`` 的映射值。"""

    value: Decimal = Field(description="变量的十进制定值")
    unit: str = Field(min_length=1, max_length=200, description="变量单位表达式")

    @field_validator("value", mode="before")
    @classmethod
    def _coerce_value(cls, v: Any) -> Any:
        return _coerce_value(v)

    @field_validator("value")
    @classmethod
    def _check_value(cls, d: Decimal) -> Decimal:
        return _check_value(d)


_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,31}$")


class EvaluateRequest(CustomUnitsSelectionModel):
    """POST /evaluate 请求体。"""

    variables: dict[str, Variable] = Field(
        min_length=1, max_length=20, description="变量映射（1～20 个），键为公式中的变量名"
    )
    formula: str = Field(min_length=1, max_length=500, description="求值公式")
    output_unit: str = Field(min_length=1, max_length=200, description="结果目标单位表达式")

    @field_validator("variables")
    @classmethod
    def _check_variable_names(cls, variables: dict[str, Variable]) -> dict[str, Variable]:
        for name in variables:
            if not _VAR_NAME_RE.match(name):
                raise ValueError(
                    f"变量名 {name!r} 不合法：须以字母或下划线开头，"
                    "仅含字母、数字、下划线，长度不超过 32"
                )
        return variables


class SolveRequest(CustomUnitsSelectionModel):
    """POST /solve 请求体。"""

    variables: dict[str, Variable] = Field(
        default_factory=dict,
        max_length=20,
        description="变量映射（0～20 个，可省略），键为方程中的变量名；不得包含 solve_for",
    )
    equation: str = Field(
        min_length=1, max_length=500, description="含恰好一个等号的方程"
    )
    solve_for: str = Field(min_length=1, max_length=32, description="待解未知量的名称")
    output_unit: str = Field(
        min_length=1, max_length=200, description="结果目标单位表达式（须为线性单位）"
    )

    @field_validator("variables")
    @classmethod
    def _check_variable_names(cls, variables: dict[str, Variable]) -> dict[str, Variable]:
        for name in variables:
            if not _VAR_NAME_RE.match(name):
                raise ValueError(
                    f"变量名 {name!r} 不合法：须以字母或下划线开头，"
                    "仅含字母、数字、下划线，长度不超过 32"
                )
        return variables

    @field_validator("equation")
    @classmethod
    def _check_single_equals(cls, equation: str) -> str:
        if equation.count("=") != 1:
            raise ValueError("equation 必须恰好包含一个等号 '='")
        return equation

    @field_validator("solve_for")
    @classmethod
    def _check_solve_for_name(cls, name: str) -> str:
        if not _VAR_NAME_RE.match(name):
            raise ValueError(
                f"未知量名称 {name!r} 不合法：须以字母或下划线开头，"
                "仅含字母、数字、下划线，长度不超过 32"
            )
        return name

    @model_validator(mode="after")
    def _check_solve_for_not_in_variables(self) -> "SolveRequest":
        if self.solve_for in self.variables:
            raise ValueError(f"solve_for {self.solve_for!r} 不得出现在 variables 中")
        return self


class ConvertResponse(BaseModel):
    """POST /convert、POST /evaluate 与 POST /solve 的成功响应。"""

    value: str = Field(description="换算结果的精确十进制表示")
    unit: str = Field(description="目标单位表达式的规范形式")
    dimension: str = Field(description="规范量纲表达式（SI 基本单位）")


class PublishUnitSetRequest(BaseModel):
    """POST /custom-unit-sets 请求体：提交 1～6 项 custom_units 格式定义。"""

    model_config = ConfigDict(populate_by_name=True)

    custom_units: CustomUnits = Field(
        min_length=1,
        max_length=6,
        description="待发布的自定义线性单位声明（1～6 项），格式与三个换算接口"
        "的请求级 custom_units 完全一致；发布前按现有规则校验并展开全部条目",
    )


class PublishedCustomUnitItem(BaseModel):
    """已发布定义集回显的单条定义（factor 为精确十进制字符串）。"""

    symbol: str = Field(description="自定义单位符号")
    factor: str = Field(description="正十进制比例：1 symbol = factor 个 base_unit")
    base_unit: str = Field(description="基准单位表达式")


class PublishedUnitSetResponse(BaseModel):
    """POST /custom-unit-sets 的成功响应。"""

    id: str = Field(description="服务生成的定义集标识（不可猜测；已发布内容不可修改）")
    custom_units: list[PublishedCustomUnitItem] = Field(
        description="已发布的定义项（factor 回显为精确十进制字符串），顺序与提交一致"
    )


class ErrorDetail(BaseModel):
    error: str = Field(description="机器可读错误码")
    message: str = Field(description="人类可读错误信息")


class ErrorResponse(BaseModel):
    detail: ErrorDetail
