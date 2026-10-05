# 量纲表达式与仿射单位换算 API

基于 **Python + FastAPI + Pydantic** 的单位换算服务：解析单位表达式（乘、除、整数幂），
规范化量纲，支持 SI 词头与摄氏/华氏/开尔文仿射温标；`/evaluate` 支持对带单位的变量
公式做量纲感知的求值，`/solve` 支持用带单位的等式反求一个未知量。所有结果均为**精确十进制**。

## 特性

- **表达式解析**：`kg*m/s^2`、`km/h`、`(m/s)^2`、`J/(kg*K)`，支持 `*`、`/`、`^`（或 `**`）、括号与整数幂
- **变量公式求值（`POST /evaluate`）**：1～20 个带 `value`/`unit` 的变量 + 公式（变量、十进制定值、`+ - * /`、括号、整数幂），解析时以 `Fraction` 同步合并数值与量纲，结果精确换算到 `output_unit`
- **等式求解（`POST /solve`）**：用带单位的等式反求未知量：两侧以 `Fraction` 同步构建未知量的一次系数与常数项并校验量纲，支持 `+ - * /`、括号与纯常数幂，解精确换算到线性的 `output_unit`
- **量纲规范化**：输出规范单位表达式与 SI 基本量纲表达式，相同输入永远产生相同输出
- **SI 词头**：`Y Z E P T G M k h da d c m u µ n p f a z y`（如 `km`、`mV`、`µm`）
- **仿射温标**：`degC`（°C）、`degF`（°F）、`K` 互为绝对温度换算；温差单位 `dC`/`dF`/`dK` 严格隔离，**禁止温差与绝对温度混算**；公式中的绝对温标变量只能作为唯一项
- **精确十进制**：内部使用有理数精确运算；结果不能表示为有限十进制或超过精度上限时返回 422，绝不输出舍入值
- **请求级自定义单位**：三个接口均可携带 1～6 项 `custom_units`（`symbol`/正十进制 `factor`/`base_unit`，含义为 `1 symbol = factor 个 base_unit`），支持前向引用与链式定义；自定义符号不与内置单位/词头冲突、不自动获得词头、不能引用 `degC`/`degF`/裸 `K`，且仅在本次请求内有效（不修改全局单位表，并发安全）
- **可复用定义集发布（`POST /custom-unit-sets`）**：把 1～6 项已按上述规则校验并展开的定义发布为**可复用定义集**，服务生成不可猜测的标识；已发布内容不可修改，三个接口可凭 `custom_unit_set_id` 选用（与请求内 `custom_units` 互斥），定义落盘保存，**容器重建后仍可使用**
- **活动别名（`GET`/`PUT /custom-unit-set-aliases`）**：为已发布的不可变定义集设置一个长期不变的**别名**：调用方始终沿用同一名称，维护者把别名切换到新发布的定义集。切换采用乐观并发（`expected_id`，初建为 `null`）：当前指向不一致返回 **409**、目标不存在返回 **422**，均不改写原指向；别名在请求开始时解析一次，三个接口可传 `custom_unit_set_alias`（与 `custom_units`、`custom_unit_set_id` 互斥），与定义集共同落盘，**容器重建后仍有效**
- **统一 422**：维度不兼容、未知变量/符号、非法幂、除零、非法仿射用法、非线性方程、零系数、精度溢出均返回 422 且不产出结果

## 快速开始

### Docker Compose（单条命令）

```bash
docker compose up --build
```

服务监听 **8000** 端口：

- API 文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

### 仅 Docker

```bash
docker build -t unit-convert-api .
docker run --rm -p 8000:8000 unit-convert-api
```

### 本地开发

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000
pytest            # 运行测试
```

## API 说明

### `POST /convert`

请求体：

| 字段    | 类型          | 说明                                       |
| ------- | ------------- | ------------------------------------------ |
| `value` | 十进制数/字符串 | 定值；建议用字符串传递以保证十进制精确性 |
| `from`  | 字符串        | 源单位表达式                               |
| `to`    | 字符串        | 目标单位表达式                             |
| `custom_units` | 数组（可省略） | 1～6 项请求级自定义单位声明，详见下文；与 `custom_unit_set_id`、`custom_unit_set_alias` 互斥 |
| `custom_unit_set_id` | 字符串（可省略） | 已发布自定义单位定义集标识，详见下文；不存在返回 422；与 `custom_units`、`custom_unit_set_alias` 互斥 |
| `custom_unit_set_alias` | 字符串（可省略） | 已发布定义集的活动别名，详见下文；请求开始解析一次，别名不存在返回 422；与 `custom_units`、`custom_unit_set_id` 互斥 |

成功响应 `200`：

| 字段        | 说明                                 |
| ----------- | ------------------------------------ |
| `value`     | 换算结果的精确十进制表示（字符串）   |
| `unit`      | 目标单位表达式的规范形式             |
| `dimension` | 规范量纲表达式（SI 基本单位）        |

```bash
curl -X POST http://localhost:8000/convert \
  -H 'Content-Type: application/json' \
  -d '{"value": "36", "from": "km/h", "to": "m/s"}'
```

```json
{"value": "10", "unit": "m/s", "dimension": "m/s"}
```

更多示例：

```bash
# 复合量纲 -> 导出单位
{"value": "1",    "from": "kg*m/s^2", "to": "N"}        -> {"value": "1",     "unit": "N",    "dimension": "kg*m/s^2"}
# 仿射温标
{"value": "98.6", "from": "degF",     "to": "degC"}     -> {"value": "37",    "unit": "degC", "dimension": "K"}
{"value": "0",    "from": "degC",     "to": "K"}        -> {"value": "273.15","unit": "K",    "dimension": "K"}
# 温差（线性）
{"value": "1",    "from": "dC",       "to": "dF"}       -> {"value": "1.8",   "unit": "dF",   "dimension": "K"}
# 规范化：乱序输入得到规范输出
{"value": "1",    "from": "s^-2*kg*m","to": "N"}        -> {"value": "1",     "unit": "N",    "dimension": "kg*m/s^2"}
```

### 请求级自定义单位（三个接口通用）

请求体可携带 `custom_units`，每项为 `{"symbol", "factor", "base_unit"}`，
含义为 **1 symbol = factor 个 base_unit**（`factor` 为正十进制数，建议用字符串传递）。
源、目标及变量单位都按**同一组定义**解析；允许在定义中前向引用本次尚未声明完的符号，
成功响应的 `unit` 保留自定义符号的规范表达式，`dimension` 仍为 SI 基本量纲。

```bash
curl -X POST http://localhost:8000/convert \
  -H 'Content-Type: application/json' \
  -d '{
    "value": "10",
    "from": "ft",
    "to": "m",
    "custom_units": [{"symbol": "ft", "factor": "0.3048", "base_unit": "m"}]
  }'
```

```json
{"value": "3.048", "unit": "m", "dimension": "m"}
```

链式定义（`furlong` 引用后声明的 `chain`，声明顺序无关）：

```bash
# 2 furlong = 2 * 10 chain = 20 * 22 m = 440 m
{"value": "2", "from": "furlong", "to": "m",
 "custom_units": [
   {"symbol": "furlong", "factor": "10",  "base_unit": "chain"},
   {"symbol": "chain",   "factor": "22",  "base_unit": "m"}
 ]}
-> {"value": "440", "unit": "m", "dimension": "m"}

# 自定义单位之间也可以互相换算
{"value": "1", "from": "mile", "to": "ft",
 "custom_units": [
   {"symbol": "mile", "factor": "1609.344", "base_unit": "m"},
   {"symbol": "ft",   "factor": "0.3048",   "base_unit": "m"}
 ]}
-> {"value": "5280", "unit": "ft", "dimension": "m"}

# 结果单位保留自定义符号的规范表达式（正指数排序，负指数以 / 连接）
{"value": "1", "from": "N*bar", "to": "kg*bar*m/s^2",
 "custom_units": [{"symbol": "bar", "factor": "100000", "base_unit": "Pa"}]}
-> {"value": "1", "unit": "bar*kg*m/s^2", "dimension": "kg^2/s^4"}
```

`/evaluate` 与 `/solve` 的用法相同——变量单位与 `output_unit` 也在该组定义下解析：

```bash
# /evaluate：以“打”为计数单位（无量纲自定义单位）
{"variables":{"a":{"value":"2","unit":"dozen"},"b":{"value":"3","unit":"dozen"}},
 "formula":"a*b","output_unit":"dozen^2",
 "custom_units":[{"symbol":"dozen","factor":"12","base_unit":"1"}]}
-> {"value":"6","unit":"dozen^2","dimension":"1"}

# /solve：x rod -> m
{"variables":{"a":{"value":"3","unit":"rod"}},
 "equation":"x = a","solve_for":"x","output_unit":"m",
 "custom_units":[{"symbol":"rod","factor":"5.0292","base_unit":"m"}]}
-> {"value":"15.0876","unit":"m","dimension":"m"}

# 温度维度只能走线性基准（带词头的 K 或 dC/dF/dK），自定义单位本身是线性的
{"value":"3","from":"mdeg","to":"mK",
 "custom_units":[{"symbol":"mdeg","factor":"2","base_unit":"mK"}]}
-> {"value":"6","unit":"mK","dimension":"K"}
```

**自定义单位规则**：

- 数量为 1～6 项；同一请求内 `symbol` 不得重名；不同请求可对同名符号给出不同定义，
  并发请求也各按本次定义计算，**不会修改全局单位表**；省略 `custom_units` 时一切照旧
- `symbol` 须以字母开头、仅含字母/数字/下划线（≤ 32 字符）；不得与内置单位、别名或
  **任何可由 SI 词头解析的符号**冲突：`m`、`kg`、`km`、`MHz`、`mL`、`l`、`°C` 等均不可声明；
  纯词头字面量 `k`、`da` 解析不出单位，可以声明
- 自定义符号**不自动获得词头**：声明了 `foo` 后 `kfoo` 仍是未知符号
- `base_unit` 为单位表达式，可引用本次声明的其他自定义符号（允许前向引用与任意长度的链），
  但**不得引用 `degC`、`degF` 或裸 `K`**（包括 `1/K`、`kg*degC` 等组合）；
  温度维度请使用 `dC`/`dF`/`dK` 或带词头的 `K`（如 `mK`），此时自定义单位是纯线性的
- 重名、循环引用（含自引用）、未知引用、非法比例（非正数/非有限十进制/超过 50 位有效数字）、
  非法符号或禁引温标均使**整次请求返回 422**——即使该符号没有被源/目标表达式实际使用

### 发布可复用的自定义单位定义集

通过 `POST /custom-unit-sets` 可把 1～6 项**现有 `custom_units` 格式**的定义发布为可复用的
定义集。服务端会先按上一节的全部规则**校验并展开每一个条目**（链式/前向引用同样立即展开）：

- 任一条目非法（重名、与内置单位/词头冲突、循环、未知引用、非法比例、禁引温标等）
  整次发布返回 **422**，**不会生成标识、不会留下任何可引用的定义集**（也不写盘）；
- 成功返回 **201** 与服务生成的标识 `id`（不可猜测），同时回显已发布条目，
  `factor` 为精确十进制字符串；
- **已发布内容不可修改**：服务不提供更新/删除接口；以相同内容再次发布会得到一个**新标识**，
  旧标识含义不变。

```bash
curl -X POST http://localhost:8000/custom-unit-sets \
  -H 'Content-Type: application/json' \
  -d '{
    "custom_units": [
      {"symbol": "furlong", "factor": "10",  "base_unit": "chain"},
      {"symbol": "chain",   "factor": "22",  "base_unit": "m"},
      {"symbol": "ft",      "factor": "0.3048", "base_unit": "m"}
    ]
  }'
```

```json
{
  "id": "cus_82327f8def2025cf00cd703f12b95c8c",
  "custom_units": [
    {"symbol": "furlong", "factor": "10",     "base_unit": "chain"},
    {"symbol": "chain",   "factor": "22",     "base_unit": "m"},
    {"symbol": "ft",      "factor": "0.3048", "base_unit": "m"}
  ]
}
```

之后在 `/convert`、`/evaluate`、`/solve` 请求体中携带 `custom_unit_set_id` 即可选用该组定义，
**一次调用的源、目标与变量单位都按同一份定义解析**：

```bash
# 2 furlong = 2 * 10 chain = 20 * 22 m = 440 m
curl -X POST http://localhost:8000/convert \
  -H 'Content-Type: application/json' \
  -d '{
    "value": "2",
    "from": "furlong",
    "to": "m",
    "custom_unit_set_id": "cus_82327f8def2025cf00cd703f12b95c8c"
  }'
```

```json
{"value": "440", "unit": "m", "dimension": "m"}
```

`/evaluate`、`/solve` 用法相同（变量单位与 `output_unit` 也在该定义集下解析）：

```bash
# /evaluate
curl -X POST http://localhost:8000/evaluate \
  -H 'Content-Type: application/json' \
  -d '{
    "variables": {"n": {"value": "10", "unit": "ft"}, "w": {"value": "2", "unit": "ft"}},
    "formula": "n*w",
    "output_unit": "ft^2",
    "custom_unit_set_id": "<已发布的标识>"
  }'
# -> {"value": "20", "unit": "ft^2", "dimension": "m^2"}

# /solve
curl -X POST http://localhost:8000/solve \
  -H 'Content-Type: application/json' \
  -d '{
    "variables": {"a": {"value": "3", "unit": "ft"}},
    "equation": "x = a",
    "solve_for": "x",
    "output_unit": "m",
    "custom_unit_set_id": "<已发布的标识>"
  }'
# -> {"value": "0.9144", "unit": "m", "dimension": "m"}
```

**选用规则**：`custom_units` 与 `custom_unit_set_id` 互斥，同时出现整次请求
返回 422；二者都缺省时三个接口保持原有行为（三种来源 `custom_units` /
`custom_unit_set_id` / `custom_unit_set_alias` 的完整选用规则见下文
[“为已发布定义集设置活动别名”](#为已发布定义集设置活动别名切换实际定义而不改调用方名称)
一节）。

- 不同定义集允许对**同名符号**给出不同含义；每次调用都构造独立的解析上下文，
  并发调用互不污染，也不会改写全局单位表；
- 定义集保存在数据目录（默认 `/data`，可用环境变量 `UNIT_API_DATA_DIR` 覆盖）的
  `custom_unit_sets.json` 中；Docker Compose 已把命名卷 `unit_sets_data` 挂载到该目录，
  **容器重建/进程重启后已发布的定义集仍可使用**；
- 示例：发布一组长度定义后把服务停掉再 `docker compose up`，原 `id` 依然可用于换算；
  重启后还可继续发布新定义集，旧定义不受影响。

### 为已发布定义集设置活动别名（切换实际定义而不改调用方名称）

已发布的定义集**不可变**：要修正定义，只能再发布一份新定义集并拿到一个**新标识**。
为了让调用方无需修改请求中的名称，服务支持在不可变定义集之上建立**活动别名**：
调用方始终传同一个 `custom_unit_set_alias`，维护者通过切换接口改变别名实际指向的
定义集标识。

**查询别名**：

```bash
# 全部别名及其当前指向（按别名排序）
curl http://localhost:8000/custom-unit-set-aliases
# {"aliases":[{"alias":"lengths","custom_unit_set_id":"cus_..."}]}

# 单个别名（不存在返回 422）
curl http://localhost:8000/custom-unit-set-aliases/lengths
# {"alias":"lengths","custom_unit_set_id":"cus_82327f8def2025cf00cd703f12b95c8c"}
```

**创建/切换别名**（`PUT /custom-unit-set-aliases/{alias}`），请求体为
`{"custom_unit_set_id", "expected_id"}`，采用**乐观并发**：

- 仅当别名当前指向与 `expected_id` 一致时切换才生效；**初建别名时当前没有指向，
  `expected_id` 必须为 `null`**；
- 当前指向与 `expected_id` 不一致时返回 **409**（响应体中带回服务端 `current_id`，
  可重新查询后重试），**不改写原指向**；
- 目标定义集标识不存在（或别名名称非法）时返回 **422**，同样**不改变任何指向**；
- 别名名称仅允许字母、数字、下划线与连字符，长度 1～64。

```bash
# 1) 发布第一版（不可变）并初建别名 lengths 指向它（expected_id 为 null）
curl -X POST http://localhost:8000/custom-unit-sets \
  -H 'Content-Type: application/json' \
  -d '{"custom_units":[{"symbol":"ft","factor":"0.3048","base_unit":"m"}]}'
# {"id":"cus_v1_id", ...}

curl -X PUT http://localhost:8000/custom-unit-set-aliases/lengths \
  -H 'Content-Type: application/json' \
  -d '{"custom_unit_set_id":"cus_v1_id","expected_id":null}'
# 200 {"alias":"lengths","custom_unit_set_id":"cus_v1_id"}

# 2) 调用方始终使用别名，无需知道背后的标识
curl -X POST http://localhost:8000/convert \
  -H 'Content-Type: application/json' \
  -d '{"value":"10","from":"ft","to":"m","custom_unit_set_alias":"lengths"}'
# {"value":"3.048","unit":"m","dimension":"m"}

# 3) 维护者发布修正后的第二版（旧版定义与标识保持不变）
curl -X POST http://localhost:8000/custom-unit-sets \
  -H 'Content-Type: application/json' \
  -d '{"custom_units":[{"symbol":"ft","factor":"0.30480","base_unit":"m"}]}'
# {"id":"cus_v2_id", ...}

# 4) 携带“当前指向” cus_v1_id 作为 expected_id 完成切换；
#    若期间已被别人切换（当前不再是 cus_v1_id），本次返回 409 且不生效
curl -X PUT http://localhost:8000/custom-unit-set-aliases/lengths \
  -H 'Content-Type: application/json' \
  -d '{"custom_unit_set_id":"cus_v2_id","expected_id":"cus_v1_id"}'
# 200 {"alias":"lengths","custom_unit_set_id":"cus_v2_id"}
```

冲突与非法目标示例：

```bash
# expected_id 与当前指向不一致 -> 409，原指向不变
PUT /custom-unit-set-aliases/lengths
{"custom_unit_set_id":"cus_v3_id","expected_id":"cus_v1_id"}
# 409 {"detail":{"error":"alias_conflict",
#       "message":"别名 'lengths' 当前指向与 expected_id 不一致：……",
#       "current_id":"cus_v2_id"}}

# 目标定义集不存在 -> 422，原指向不变
PUT /custom-unit-set-aliases/lengths
{"custom_unit_set_id":"cus_does_not_exist","expected_id":"cus_v2_id"}
# 422 {"detail":{"error":"invalid_request","message":"别名 'lengths' 无法绑定到不存在的定义集: ……"}}
```

**用别名调用三个计算接口**：在 `/convert`、`/evaluate`、`/solve` 请求体中传
`custom_unit_set_alias` 即可。别名在**请求开始时解析一次**，本次请求的源、目标与
变量单位始终使用解析到的那一版定义；即使别名在请求处理过程中被切换，本次请求也会
**完整使用旧版或新版**，不会跨版本混用。别名不存在整次请求返回 **422**。

```bash
# /evaluate
curl -X POST http://localhost:8000/evaluate \
  -H 'Content-Type: application/json' \
  -d '{
    "variables": {"n": {"value": "10", "unit": "ft"}, "w": {"value": "2", "unit": "ft"}},
    "formula": "n*w",
    "output_unit": "ft^2",
    "custom_unit_set_alias": "lengths"
  }'
# -> {"value": "20", "unit": "ft^2", "dimension": "m^2"}

# /solve
curl -X POST http://localhost:8000/solve \
  -H 'Content-Type: application/json' \
  -d '{
    "variables": {"a": {"value": "3", "unit": "ft"}},
    "equation": "x = a",
    "solve_for": "x",
    "output_unit": "m",
    "custom_unit_set_alias": "lengths"
  }'
# -> {"value": "0.9144", "unit": "m", "dimension": "m"}
```

**别名规则**：

| 请求中的字段                         | 行为                                                             |
| ------------------------------------ | ---------------------------------------------------------------- |
| 只有 `custom_units`                  | 沿用请求级定义（原有行为，仅本次请求有效）                        |
| 只有 `custom_unit_set_id`            | 直接按该已发布定义集解析（原有行为）；标识不存在返回 422          |
| 只有 `custom_unit_set_alias`         | 请求开始解析一次别名并全程使用该版定义；**别名不存在返回 422**    |
| 三者中任意两个（或三个）同时出现     | **整次请求返回 422**（一次调用只能使用一种自定义单位来源）        |
| 三者都不带                           | 三个接口保持原有行为与响应，不使用任何自定义单位                  |

- 切换别名与落盘**共同生效**：内存指向更新与 JSON 原子写盘在同一临界区完成，
  写盘失败会回滚进程内指向（不改变原指向）后返回错误；
- 别名与定义集保存在同一个 `custom_unit_sets.json` 中，命名卷挂载方式不变，
  **容器重建/进程重启后别名仍然有效**；更早版本写出的、没有别名字段的旧文件
  也能正常加载（按空别名处理，原定义集照常可用）；
- 别名只是不可变定义集之上的一层引用：旧标识含义永不改变，切换别名不影响任何
  直接使用 `custom_unit_set_id` 的调用；不选别名（也不带其他自定义来源）的
  请求行为与以前完全一致。

### `POST /evaluate`

对带单位的变量映射与公式求值，并把结果**精确换算**到 `output_unit`。

请求体：

| 字段          | 类型   | 说明                                                        |
| ------------- | ------ | ----------------------------------------------------------- |
| `variables`   | 对象   | 1～20 个变量映射；键为变量名，值为 `{"value", "unit"}`      |
| `formula`     | 字符串 | 求值公式（见下文语法）                                      |
| `output_unit` | 字符串 | 结果目标单位表达式                                          |
| `custom_units`| 数组（可省略） | 1～6 项请求级自定义单位声明，详见上文；与 `custom_unit_set_id`、`custom_unit_set_alias` 互斥 |
| `custom_unit_set_id` | 字符串（可省略） | 已发布自定义单位定义集标识；不存在返回 422；与 `custom_units`、`custom_unit_set_alias` 互斥 |
| `custom_unit_set_alias` | 字符串（可省略） | 已发布定义集的活动别名；请求开始解析一次，别名不存在返回 422；与 `custom_units`、`custom_unit_set_id` 互斥 |

变量名须以字母或下划线开头，仅含字母、数字、下划线（≤32 字符）；
`value` 同样建议以 JSON 字符串传递以保证十进制精确性。成功响应与 `/convert` 相同。

```bash
curl -X POST http://localhost:8000/evaluate \
  -H 'Content-Type: application/json' \
  -d '{
    "variables": {
      "v": {"value": "60", "unit": "km/h"},
      "t": {"value": "2",  "unit": "h"}
    },
    "formula": "v*t",
    "output_unit": "km"
  }'
```

```json
{"value": "120", "unit": "km", "dimension": "m"}
```

更多示例：

```bash
# 同量纲相加（自动折算不同词头）
{"variables":{"a":{"value":"1","unit":"m"},"b":{"value":"50","unit":"cm"}},
 "formula":"a+b","output_unit":"m"}
-> {"value":"1.5","unit":"m","dimension":"m"}

# 牛顿第二定律
{"variables":{"m":{"value":"2","unit":"kg"},"a":{"value":"3","unit":"m/s^2"}},
 "formula":"m*a","output_unit":"N"}
-> {"value":"6","unit":"N","dimension":"kg*m/s^2"}

# (v^2-u^2)/(2*a)，负指数、括号、十进制定值均可
{"variables":{"v":{"value":"20","unit":"m/s"},"u":{"value":"10","unit":"m/s"},
              "a":{"value":"5","unit":"m/s^2"}},
 "formula":"(v^2-u^2)/(2*a)","output_unit":"m"}
-> {"value":"30","unit":"m","dimension":"m"}

# 绝对温标变量只能作为公式唯一项（可整体加括号），然后正常仿射换算
{"variables":{"T":{"value":"98.6","unit":"degF"}},"formula":"T","output_unit":"degC"}
-> {"value":"37","unit":"degC","dimension":"K"}

# 温差是线性量，可以加减
{"variables":{"a":{"value":"10","unit":"dC"},"b":{"value":"5","unit":"dC"}},
 "formula":"a-b","output_unit":"dF"}
-> {"value":"9","unit":"dF","dimension":"K"}
```

**求值规则**：

- 解析时用 `Fraction` 同步合并数值与量纲：乘除量纲指数加减；只有**同量纲**项才能相加/相减
- 幂指数必须是**无量纲整数**（字面整数或无量纲整数变量，|n| ≤ 99），如 `x^2`、`f^-1`；`2^x`（x 有量纲）、`x^0.5` 返回 422
- 不允许隐式相乘：`2 x`、`x y`、`x(y)` 均为语法错误
- 除零（含 `0^-n`）返回 422 `division_by_zero`
- 绝对温标变量（单位为裸 `degC`/`degF`/`K`）**只能作为公式唯一项**：`T+a`、`T*2`、`-T`、`T^1`、`T/1` 均返回 422 `affine_mixing`；且其结果只能换算到绝对温标（不能换算到 `dC`/`dF`/`dK`、`mK` 或复合单位）
- 温度维度下，温差变量（`dC`/`dF`/`dK`、带词头 K 等）可以正常参与线性运算；温差结果不能换算到绝对温标
- 公式中的十进制定值视为无量纲，有效数字同样受 50 位上限约束

### `POST /solve`

用带单位的等式反求未知量 `solve_for`：等式两侧分别解析为关于未知量的一次仿射形式
（`Fraction` 精确系数 + 常数项，量纲同步校验），解
`(左系数 - 右系数)·x = 右常数 - 左常数`，并把解**精确换算**到 `output_unit`。

请求体：

| 字段          | 类型   | 说明                                                        |
| ------------- | ------ | ----------------------------------------------------------- |
| `variables`   | 对象   | 0～20 个变量映射（可省略）；键为变量名，不得包含 `solve_for` |
| `equation`    | 字符串 | 含**恰好一个等号**的等式，两侧语法同 `/evaluate` 的公式      |
| `solve_for`   | 字符串 | 待解未知量的名称（不得出现在 `variables` 中）                |
| `output_unit` | 字符串 | 结果目标单位表达式，**须为线性单位**（不能是 `degC`/`degF`） |
| `custom_units`| 数组（可省略） | 1～6 项请求级自定义单位声明，详见上文；与 `custom_unit_set_id`、`custom_unit_set_alias` 互斥 |
| `custom_unit_set_id` | 字符串（可省略） | 已发布自定义单位定义集标识；不存在返回 422；与 `custom_units`、`custom_unit_set_alias` 互斥 |
| `custom_unit_set_alias` | 字符串（可省略） | 已发布定义集的活动别名；请求开始解析一次，别名不存在返回 422；与 `custom_units`、`custom_unit_set_id` 互斥 |

成功响应与 `/convert` 相同（`value`/`unit`/`dimension`）。

```bash
curl -X POST http://localhost:8000/solve \
  -H 'Content-Type: application/json' \
  -d '{
    "variables": {
      "v": {"value": "60",  "unit": "km/h"},
      "s": {"value": "120", "unit": "km"}
    },
    "equation": "v*t = s",
    "solve_for": "t",
    "output_unit": "h"
  }'
```

```json
{"value": "2", "unit": "h", "dimension": "s"}
```

更多示例：

```bash
# 无需变量的纯数值方程
{"equation":"2*x = 10","solve_for":"x","output_unit":"1"}
-> {"value":"5","unit":"1","dimension":"1"}

# 牛顿第二定律反求加速度
{"variables":{"F":{"value":"6","unit":"N"},"m":{"value":"2","unit":"kg"}},
 "equation":"F = m*a","solve_for":"a","output_unit":"m/s^2"}
-> {"value":"3","unit":"m/s^2","dimension":"m/s^2"}

# 未知量可出现在等式两侧
{"variables":{"a":{"value":"3","unit":"m"},"b":{"value":"7","unit":"m"}},
 "equation":"x + a = b - x","solve_for":"x","output_unit":"m"}
-> {"value":"2","unit":"m","dimension":"m"}

# 温差是线性量，可正常参与求解
{"variables":{"a":{"value":"5","unit":"dC"},"b":{"value":"12","unit":"dC"}},
 "equation":"x + a = b","solve_for":"x","output_unit":"dF"}
-> {"value":"12.6","unit":"dF","dimension":"K"}

# 零值因子/相消子式掩盖不了未知量：结构非线性，返回 422
{"equation":"0*x*x+x = 1","solve_for":"x","output_unit":"1"}
-> 422 {"detail":{"error":"nonlinear_equation",
        "message":"含未知量的子式相乘导致次数超过一次（非线性）"}}

{"equation":"(x-x)^2+x = 1","solve_for":"x","output_unit":"1"}
-> 422 {"detail":{"error":"nonlinear_equation",
        "message":"未知量不能参与幂运算（非线性）"}}
```

**求解规则**：

- `equation` 必须恰好含一个 `=`；`solve_for` 不得出现在 `variables` 中（违反返回 422 `invalid_request`）
- 未知量只能处于**一次线性位置**：`x*x`、`(x+1)*(x-1)`、`1/x`、`x^2`、`2^x` 等均返回 422 `nonlinear_equation`；幂只允许作用于纯常数子式（如 `2^3`、`2^-2`）
- 是否“含未知量”按**结构**判定：零值因子（`0*x`）或相消子式（`x-x`）即使数值为 0 也掩盖不了未知量，`0*x*x`、`(x-x)^2`、`1/(x-x)` 同样返回 422 `nonlinear_equation`；加减、取负与括号嵌套都会保留这一结构标记
- 未知量系数为零时方程**无解**（两侧常数项不相等，如 `x = x+1`）或**有无穷多解**（恒等式，如 `x = x`），均返回 422 `no_unique_solution`
- 加减与等式两侧的量纲同步校验；解与 `output_unit` 量纲不兼容返回 422 `dimension_mismatch`
- 绝对温标变量（裸 `degC`/`degF`/`K`）不能出现在等式中（422 `affine_mixing`）；`output_unit` 必须是线性单位（不能是 `degC`/`degF`）
- 除零返回 422 `division_by_zero`；解不是有限十进制或超过精度上限返回 422 `precision_overflow`
- 解恒为零且量纲不受方程约束时（如 `2*x = x`），接受任意线性目标单位，结果为 `0`

### 其他端点

| 端点         | 说明                             |
| ------------ | -------------------------------- |
| `POST /custom-unit-sets` | 发布 1～6 项自定义单位定义为可复用定义集，返回服务生成标识（201） |
| `GET /custom-unit-set-aliases` | 列出全部活动别名及其当前指向的定义集标识 |
| `GET /custom-unit-set-aliases/{alias}` | 查询单个活动别名当前指向（不存在返回 422） |
| `PUT /custom-unit-set-aliases/{alias}` | 创建/切换活动别名（乐观并发：`expected_id` 初建为 `null`）；成功 200，冲突 409，目标不存在 422 |
| `GET /units` | 列出全部单位、词头与别名         |
| `GET /health`| 健康检查                         |
| `GET /`      | 服务信息                         |

## 表达式语法

### 单位表达式（`from`/`to`/`output_unit`/变量 `unit`）

```
expr    := term (("*" | "/") term)*
term    := factor (("^" | "**") exponent)?
factor  := "(" expr ")" | SYMBOL | "1"
exponent:= ["("] ["+" | "-"] INTEGER [")"]
```

- 不允许隐式相乘：`kgm` 是未知符号，必须写 `kg*m`
- 表达式中只允许数字 `1`（表示无量纲）
- 幂指数必须为整数且 |n| ≤ 99：`m^2`、`s^-2`、`m**3`、`m^(2)` 均可；`m^0.5` 返回 422

### 公式（`/evaluate` 的 `formula`）

```
expr   := term (("+" | "-") term)*
term   := unary (("*" | "/") unary)*
unary  := ("+" | "-") unary | factor
factor := PRIMARY (("^" | "**") unary)?    # 幂右结合
PRIMARY:= "(" expr ")" | VARIABLE | DECIMAL
```

- 运算符优先级：括号 > 一元正负号与幂（`-x^2` = `-(x^2)`，幂右结合：`2^3^2` = 512）> `*` `/` > `+` `-`
- 指数位置允许一元负号：`x^-2`；十进制定值如 `2`、`0.5` 为无量纲精确分数
- 不允许隐式相乘：`2x`、`x y`、`x(y)` 均返回 422

## 温度规则

| 类别     | 单位                | 说明                                       |
| -------- | ------------------- | ------------------------------------------ |
| 绝对温度 | `degC` `degF` `K`   | 仿射换算，如 `98.6 degF -> 37 degC`        |
| 温差     | `dC` `dF` `dK`      | 纯线性换算，如 `1 dC -> 1.8 dF`            |

- `degC`/`degF` 必须**单独出现**：`kg*degC`、`degC^2`、`J/degF` 均返回 422
- 复合表达式中的温度请使用 `K`（如 `J/(kg*K)`）
- 绝对温度与温差互转（如 `degC -> dK`、`dC -> degC`）返回 422
- `K` 是中性的：既可参与绝对温度换算，也可与温差单位换算
- 在 `/evaluate` 中：单位为裸 `degC`/`degF`/`K` 的变量属于**绝对温标变量**，只能作为公式唯一项（`T` 或 `(T)`）；温差变量（`dC`/`dF`/`dK`、`mK` 等）可正常线性运算
- 在 `/solve` 中：绝对温标变量不能出现在等式中；温差变量可正常参与求解；目标单位须为线性单位
- 别名：`°C`→`degC`，`°F`→`degF`

## 支持的单位

- **SI 基本单位**：`m`、`g`（`kg` 由词头组合）、`s`、`A`、`K`、`mol`、`cd`
- **SI 导出单位**：`Hz N Pa J W C V F ohm S Wb T H lm lx Bq Gy Sv kat`、`rad`
- **常用非 SI**：`min`、`h`、`d`、`L`
- **温度**：`degC`、`degF`、`dC`、`dF`、`dK`
- **词头**：上表 SI 词头可作用于所有 `prefixable` 单位（`GET /units` 查看），如 `km`、`ms`、`mL`、`kPa`、`µm`
- **别名**：`Ω`→`ohm`，`l`/`ℓ`→`L`

## 精确性与精度上限

- 内部以分数（`Fraction`）做精确有理数运算，输出前才化为十进制
- 仅当结果是**有限十进制**且有效数字 ≤ **50 位**时输出，否则返回 422 `precision_overflow`
  - 例如 `1 degF -> K`（255.92777…，无限循环）与 `1 s -> min`（0.01666…）都会返回 422
  - 而 `32 degF -> K = 273.15`、`30 s -> min = 0.5` 可以精确输出
- 输入 `value`（及公式中的十进制定值）的有效数字同样不得超过 50 位
- 可通过环境变量调整：`UNIT_API_MAX_PRECISION`（默认 50）、`UNIT_API_MAX_POWER`（默认 99）

## 错误码

换算类错误响应体统一为 `{"detail": {"error": "<错误码>", "message": "<说明>"}}`，
**均为 HTTP 422**：

| 错误码               | 含义                                   |
| -------------------- | -------------------------------------- |
| `invalid_request`    | 请求参数校验失败（缺字段、值非法等；自定义单位的重名/符号冲突/循环引用/非法比例/禁引温标，以及 `custom_units`、`custom_unit_set_id`、`custom_unit_set_alias` 任意两个同时出现、引用的定义集标识或活动别名不存在、别名绑定的目标定义集不存在也归入此码）   |
| `invalid_syntax`     | 表达式/公式语法错误（含隐式相乘）      |
| `unknown_symbol`     | 未知单位符号（含自定义单位的 `base_unit` 引用了未声明符号） |
| `unknown_variable`   | 公式引用了未在变量映射中提供的变量     |
| `invalid_power`      | 非法幂（指数非无量纲整数或超出上限）   |
| `dimension_mismatch` | 量纲不兼容（加减项或结果与目标单位）   |
| `division_by_zero`   | 除数为零（含对零取负整数幂）           |
| `affine_mixing`      | 温差与绝对温度混算 / 仿射温标被组合    |
| `nonlinear_equation` | 未知量处于非线性位置（相乘、作除数、参与幂；按结构判定，零值因子/相消子式不豁免） |
| `no_unique_solution` | 未知量系数为零：方程无解或有无穷多解   |
| `precision_overflow` | 结果不是有限十进制或超过精度上限       |

**唯一的非 422 领域状态**：`PUT /custom-unit-set-aliases/{alias}` 在别名当前指向
与提交的 `expected_id` 不一致时返回 **HTTP 409**：

```json
{"detail": {"error": "alias_conflict",
            "message": "别名 'lengths' 当前指向与 expected_id 不一致：……",
            "current_id": "cus_..."}}
```

`current_id` 为服务端当前指向（初建冲突、当前没有指向时省略该字段）。冲突时不切换、
不落盘、不改写原指向；调用方可按 `current_id` 重新读取后决定是否重试。

## 项目结构

```
app/
  main.py       # FastAPI 应用、路由与异常处理（含别名 409）
  schemas.py    # Pydantic 请求/响应模型
  parser.py     # 单位表达式解析器（递归下降）与规范化输出
  evaluator.py  # 公式求值器（Fraction 数值/量纲合并、温标规则）
  solver.py     # 等式求解器（未知量一次系数/常数的 Fraction 构建与量纲校验）
  units.py      # 单位注册表、SI 词头与别名
  custom_units.py  # 请求级自定义单位（custom_units）：冲突/循环/禁引校验与链式解析，三种来源选择
  unit_sets.py  # 已发布定义集与活动别名：校验发布、标识生成、别名乐观切换（CAS）、线程安全与 JSON 落盘/重载
  converter.py  # 量纲校验、仿射规则与精确十进制换算
  errors.py     # 领域错误（换算类统一映射 422；别名冲突 AliasConflictError 映射 409）
  config.py     # 精度/幂次上限/数据目录配置
tests/
  test_api.py       # /convert 端到端测试
  test_evaluate.py  # /evaluate 端到端测试
  test_solve.py     # /solve 端到端测试
  test_custom_units.py  # 请求级自定义单位（三个接口）端到端测试
  test_unit_sets.py     # 可复用定义集发布/选用/互斥/隔离/不可变/持久化测试
  test_unit_set_aliases.py  # 活动别名查询/切换（409/422）/三接口选用/并发一致性/持久化与旧文件兼容测试
Dockerfile
docker-compose.yml  # 命名卷 unit_sets_data -> /data，持久化已发布定义集与活动别名
```
