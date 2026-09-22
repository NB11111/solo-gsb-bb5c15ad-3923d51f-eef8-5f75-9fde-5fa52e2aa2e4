# GS1 条码元素串 / Digital Link 解析 API

基于 Python + FastAPI 的 GS1 解析服务。`/parse` 接口同时接受两种输入：

1. **元素串（Element String）**：扫码枪原始输出（含 ASCII 29 / FNC1 分隔符）；
2. **GS1 Digital Link URI**：以 `http://` 或 `https://` 开头的链接，AI/值对位于路径
   （`/AI/值/AI/值`）和查询参数（`?AI=值&AI=值`）中。

两种输入都返回保持原顺序的结构化元素，并完成同一套字符集、长度、GTIN Mod-10
校验位与真实日期校验。重量 AI（310n/320n）在两种输入下行为完全一致，响应除
原始 `value` 外还会返回 `decimal_value`（按隐含小数位解释后的数值）与 `unit`。

## 一键启动

```bash
docker compose up --build
```

需要后台运行时，一条命令即可（守护态启动、自动构建）：

```bash
docker compose up --build -d
```

启动后通过 **8080** 端口访问：

- API 地址：http://localhost:8080
- 交互式文档（Swagger UI）：http://localhost:8080/docs
- 健康检查：http://localhost:8080/health

查看日志与停止服务：

```bash
docker compose logs -f   # 查看日志
docker compose down      # 停止
```

## 支持的 AI

| AI   | 含义        | 长度            | 字符集                          |
| ---- | ----------- | --------------- | ------------------------------- |
| 01   | GTIN        | 固定 14 位数字  | 0-9，含 Mod-10 校验位验证       |
| 17   | 效期 YYMMDD | 固定 6 位数字   | 0-9，校验真实日期（含闰年）     |
| 10   | 批号        | 可变 1-20 字符  | GS1 AI 字符集 82                |
| 21   | 序列号      | 可变 1-20 字符  | GS1 AI 字符集 82                |
| 310n | 净重（千克 kg） | AI 4 位 + 固定 6 位数字 | 0-9；`n` 为 0-9 的隐含小数位数 |
| 320n | 净重（磅 lb）   | AI 4 位 + 固定 6 位数字 | 0-9；`n` 为 0-9 的隐含小数位数 |

重量 AI 说明：

- 标识符共 4 位：前缀 `310`（千克）或 `320`（磅）+ 1 位小数位指示符 `n`
  （0-9），其后**固定 6 位数字**，按长度自动定界（与 01/17 一样无需分隔符）。
- 解析器按**最长可匹配**识别四位标识符：`310n`/`320n` 优先于两位 AI；
  例如 `3103...` 识别为 AI `3103`，而 `3110...` 仍按两位 AI `31` 处理
  （`31` 不受支持，返回 `UNSUPPORTED_AI`）。
- 值按整数除以 10^n 解释：`3103 002513` → `decimal_value = 2.513`，
  `unit = "kg"`；`3202 002513` → `25.13 lb`。`n = 0` 时为整数；
  `n > 6` 时高位补隐含的零，如 `3109 000001` → `0.000000001 kg`。
- 响应中的 `value` 保留**原始 6 位数字**（不插小数点），仅重量元素额外携带
  `decimal_value`（JSON number）与 `unit`（`"kg"` / `"lb"`）两个字段。

解析规则：

- 固定长度字段（01、17、310n、320n）按长度自动定界，无需分隔符。
- 可变长度字段（10、21）除位于串末尾外，必须以分隔符 ASCII 29（GS，扫码枪 FNC1）
  结束；JSON 中以 `\u001d` 表示。
- AI 17 的日字段为 `00` 时按 GS1 规范表示当月最后一天；年份按 20YY 解释。
- `data` 以 `http://` 或 `https://` 开头时按 GS1 Digital Link URI 解析，否则按
  元素串解析。

### Digital Link URI 规则

- 路径段必须成对出现：`/AI/值/AI/值...`；查询参数按 `AI=值&AI=值` 解析（参数没有
  等号时值为空，会触发 `FIELD_EMPTY`）。
- 值先做百分号 UTF-8 解码（`%XX`），再执行与元素串完全相同的字符集、长度、校验位与
  日期校验；`offset` 始终指向**原始 URI** 中的字节位置（解码后字符报错时定位到它的
  首个编码字节，即 `%` 所在字节）。
- 未知 AI、路径内重复 AI、查询参数内重复 AI，以及同一 AI 同时出现在路径和查询参数
  中，均返回 `422`。
- 响应中路径提取到的元素按路径顺序在前，查询参数提取到的元素按参数顺序在后。
- 片段（`#...`）不属于 Digital Link 数据，解析时忽略。

## 使用示例

### 元素串

请求（注意批号与序列号之间的 `\u001d` 分隔符）：

```bash
curl -X POST http://localhost:8080/parse \
  -H "Content-Type: application/json" \
  -d '{"data": "01095011015300031725013110ABC123\u001d21SN0001"}'
```

响应 `200 OK`，元素按原始顺序返回，`offset` 为 AI 在输入中的字节偏移：

```json
{
  "elements": [
    { "ai": "01", "name": "GTIN",     "value": "09501101530003", "offset": 0  },
    { "ai": "17", "name": "EXPIRY",   "value": "250131",         "offset": 16 },
    { "ai": "10", "name": "BATCH/LOT","value": "ABC123",         "offset": 24 },
    { "ai": "21", "name": "SERIAL",   "value": "SN0001",         "offset": 33 }
  ]
}
```

### GS1 Digital Link URI

路径和查询参数混用的请求（路径中的元素在前，查询参数中的元素在后）：

```bash
curl -X POST http://localhost:8080/parse \
  -H "Content-Type: application/json" \
  -d '{"data": "https://id.gs1.org/01/09501101530003/17/250131?10=ABC123&21=SN0001"}'
```

响应与元素串完全一致，`offset` 是 AI 在**原 URI** 中的字节偏移：

```json
{
  "elements": [
    { "ai": "01", "name": "GTIN",      "value": "09501101530003", "offset": 19 },
    { "ai": "17", "name": "EXPIRY",    "value": "250131",         "offset": 37 },
    { "ai": "10", "name": "BATCH/LOT", "value": "ABC123",         "offset": 47 },
    { "ai": "21", "name": "SERIAL",    "value": "SN0001",         "offset": 57 }
  ]
}
```

值可使用百分号编码，按 UTF-8 解码后再做同一套校验。下面的批号解码后为
`Lot A/100`（百分号编码指向原 URI 字节位置，示例中 `/` 编码为 `%2F`；
注意空格不属于字符集 82，仅作编码演示）：

```bash
curl -X POST http://localhost:8080/parse \
  -H "Content-Type: application/json" \
  -d '{"data": "https://id.gs1.org/10/Lot%20A%2F100"}'
```

该请求会返回 `422 INVALID_CHARACTER`，`offset` 指向编码空格的 `%20` 首字节。
仅含合法字符的示例：

```bash
curl -X POST http://localhost:8080/parse \
  -H "Content-Type: application/json" \
  -d '{"data": "https://id.gs1.org/10/Lot%2FA%2F100"}'
```

解码后批号为 `Lot/A/100`，返回 `200`。

路径段必须成对，例如 `.../01/09501101530003/17` 会返回 `422`
（`UNPAIRED_PATH`）；同一 AI 不能重复，也不能同时出现在路径和查询参数中
（`DUPLICATE_AI`）。

### 重量 AI（310n / 320n）

元素串与 Digital Link 两种输入返回完全相同的重量字段。元素串示例（`n=3`，
`002513` 表示 2.513 kg）：

```bash
curl -X POST http://localhost:8080/parse \
  -H "Content-Type: application/json" \
  -d '{"data": "01095011015300033103002513"}'
```

Digital Link 示例（路径中的千克重量，查询参数中的磅重量）：

```bash
curl -X POST http://localhost:8080/parse \
  -H "Content-Type: application/json" \
  -d '{"data": "https://id.gs1.org/01/09501101530003/3103/002513?3202=002513"}'
```

两种请求的重量元素结构一致，`value` 为原始 6 位数字，`decimal_value` 为按
`n` 位隐含小数点解释后的数值，`unit` 为单位：

```json
{
  "ai": "3103",
  "name": "NET WEIGHT (KG)",
  "value": "002513",
  "decimal_value": 2.513,
  "unit": "kg",
  "offset": 16
}
```

```json
{
  "ai": "3202",
  "name": "NET WEIGHT (LB)",
  "value": "002513",
  "decimal_value": 25.13,
  "unit": "lb",
  "offset": 49
}
```

非重量元素（01/10/17/21）不会出现 `decimal_value` 与 `unit` 字段。

## 批量重量汇总 `POST /weight-summary`

一次提交多条扫码字符串，按 **GTIN（AI 01）+ 批号（AI 10）** 分组汇总净重。

- 请求体：`{"scans": ["...", ...], "target_unit": "kg"}`。`target_unit`
  可选，仅接受 `"kg"` / `"lb"`，默认 `"kg"`；`scans` 中的每一项既可以是
  元素串，也可以是 GS1 Digital Link URI（与 `/parse` 完全一致）。
- 每条扫描必须包含**恰好一个 AI 01**、**零个或一个 AI 10**（批号可缺省）和
  **恰好一个重量 AI**（310n 或 320n，允许混合千克/磅）；可有可无的 AI 17/21
  会被忽略。缺少/重复 AI 01、重复 AI 10、缺少或出现多个重量 AI，或任一条
  扫描解析失败，整个请求返回 `422`，`detail.scan_index` 给出出错下标
  （从 0 开始），`detail.error` 为该条扫描的原始错误（结构同 `/parse`）。
- 重量一律使用 `Decimal` 从**原始六位数字与精度位 n** 重建（`整数 / 10^n`），
  不使用解析结果中的 float `decimal_value`，累加也全程 `Decimal`，禁止
  float 累加。换算系数固定为 **1 lb = 0.45359237 kg**（磅→千克相乘，
  千克→磅按该系数的精确倒数相除）。
- 分组按首次出现顺序返回；无批号（缺省 AI 10）的扫描归入 `batch: null`
  的独立分组。每组包含 `gtin`、`batch`、`count`、`unit` 与 `total`：
  `total` 是组内所有重量转换到 `target_unit` 后之和，最终按**六位小数
  half-even（银行家舍入）**输出为 JSON 数字（如 `2.513000`，恒为六位，
  即使为零也显示 `0.000000`）；舍入在分组累加完成后一次性进行。
- `scans` 为空数组时返回 `200` 与空的 `groups`。

请求示例（同一 GTIN+批号下一条千克、一条磅，目标单位 kg）：

```bash
curl -X POST http://localhost:8080/weight-summary \
  -H "Content-Type: application/json" \
  -d '{"target_unit": "kg", "scans": ["010950110153000310B1\u001d3103002513", "010950110153000310B1\u001d3202002513", "010950110153000310B2\u001d3106123456"]}'
```

`2.513 kg + 25.13 lb × 0.45359237 = 2.513 + 11.3987762581 = 13.9117762581`，
六位 half-even 舍入为 `13.911776`；响应 `200 OK`：

```json
{
  "target_unit": "kg",
  "groups": [
    {"gtin": "09501101530003", "batch": "B1", "count": 2, "unit": "kg", "total": 13.911776},
    {"gtin": "09501101530003", "batch": "B2", "count": 1, "unit": "kg", "total": 0.123456}
  ]
}
```

目标单位为磅时（`1 kg = 1 / 0.45359237 ≈ 2.20462262 lb`）：

```bash
curl -X POST http://localhost:8080/weight-summary \
  -H "Content-Type: application/json" \
  -d '{"target_unit": "lb", "scans": ["01095011015300033100000001"]}'
```

```json
{"target_unit": "lb", "groups": [{"gtin": "09501101530003", "batch": null, "count": 1, "unit": "lb", "total": 2.204623}]}
```

出错时（下例下标 1 的 GTIN 校验位错误）返回 `422`：

```json
{
  "detail": {
    "scan_index": 1,
    "error": {
      "error": "INVALID_CHECK_DIGIT",
      "message": "GTIN check digit mismatch: expected 3, found 4",
      "offset": 15,
      "expected": "Mod-10 check digit 3",
      "found": "4"
    }
  }
}
```

结构类错误（缺少/重复 AI）使用同样的 `detail` 结构，错误码为 `MISSING_AI`
或 `DUPLICATE_AI`，例如重复的重量 AI：

```json
{
  "detail": {
    "scan_index": 0,
    "error": {
      "error": "DUPLICATE_AI",
      "message": "scan 0: 2 weight elements (3103, 3202); exactly one 310n/320n is required",
      "offset": 26,
      "expected": "exactly one weight AI 310n (kg) or 320n (lb)",
      "found": "3103, 3202"
    }
  }
}
```

## 效期优先拣货计划 `POST /weight-pick-plan`

一次提交多条扫码字符串和一组按 GTIN 的需求，按 **FEFO（先到期先出）** 为每个
需求分配净重，并按每个需求的剩余效期门槛过滤库存。

- 请求体：`{"scans": [...], "target_unit": "kg", "as_of": "2025-01-15", "demands": [{"gtin": "...", "quantity": 5, "min_remaining_days": 30}]}`。
  `target_unit` 可选，仅接受 `"kg"` / `"lb"`，默认 `"kg"`；`as_of` **必填**，为
  `YYYY-MM-DD` 格式的真实日期（计划基准日），缺失或非法返回 `422`；`scans` 中的
  每一项既可以是元素串，也可以是 GS1 Digital Link URI（与 `/parse` 完全一致）。
- 每条扫描必须包含**恰好一个 AI 01**、**恰好一个 AI 17**（效期）、**零个或一个
  AI 10**（批号可缺省）和**恰好一个重量 AI**（310n 或 320n，允许混合千克/磅）；
  AI 21 会被忽略。任一条扫描解析失败或缺少/重复必需 AI，整个请求返回 `422`，
  `detail.scan_index` 给出出错下标（从 0 开始），`detail.error` 为该条扫描的原始
  错误（结构同 `/parse`）。
- `demands` 每项含 `gtin` 与 `quantity`（`quantity` 以 `target_unit` 计，必须大于 0，
  按 `Decimal` 精确解析，JSON number 与字符串均可），以及可选的
  `min_remaining_days`（**非负整数，默认 0**）和 `package_mode`
  （`"splittable"` / `"whole"`，**默认 `"splittable"`**）。**同一 GTIN 重复出现返回
  `422`**，`detail.demand_index` 给出出错下标；`min_remaining_days` 为负数时同样
  返回 `422` 并以 `detail.demand_index` 定位。
- 效期门槛：对每个需求，仅**实际效期不早于 `as_of + min_remaining_days` 天**的
  库存分组可参与分配（边界日期当天可用）；AI 17 日字段 `00` 仍按当月最后一天
  解释（如 `250200` = 2025-02-28，闰年 `240200` = 2024-02-29）。被门槛排除的库存
  **不计入 `fulfilled`**，缺口仍计入 `shortage`。
- 汇总与分配（`package_mode: "splittable"`，默认）：先按 **GTIN + 批号 + AI 17 原值**
  汇总可用重量（换算到 `target_unit`），再对每个需求的 GTIN 在**合格**分组中按
  **AI 17 实际日期升序**执行 FEFO；实际日期相同的分组保持批次**首次出现**顺序；
  **最后一批允许部分分配**。
- 整包模式（`package_mode: "whole"`）：把该需求 GTIN 的**每条合格扫描**视为一个
  **不可拆包裹**（不做分组汇总），沿用实际效期、保质期门槛与 `Decimal` 单位换算。
  合格包裹按 FEFO（实际效期升序，同日保持原扫描顺序）排序后，若存在总重达到
  `quantity` 的子集，选取**超量最小**者；超量相同时选取**索引序列字典序最小**者
  （索引即 FEFO 排序后的位置）；若任何子集都达不到 `quantity`，则**选取全部合格
  包裹**。单个需求的合格包裹**超过 30 个**时整个请求返回 `422`
  （`detail.demand_index` 定位，`detail.error.error` 为 `TOO_MANY_PACKAGES`）。
- 换算、汇总、比较与分配全程使用 `Decimal` 与固定换算系数
  **1 lb = 0.45359237 kg**（千克→磅按其精确倒数换算），仅在响应时按**六位小数
  half-even（银行家舍入）**输出为 JSON 数字（如 `2.513000`）。
- 响应：`target_unit` 加与请求同序的 `demands` 数组。每项含 `gtin`、`quantity`
  （回显）、`fulfilled`（已分配总量）、`shortage`（缺口，**不为负**，整包超量时为
  `0.000000`）、`overage`（整包模式超出 `quantity` 的部分，可拆模式恒为
  `0.000000`）、`excluded_weight`（同 GTIN 因门槛被排除的重量合计，以
  `target_unit` 计，无排除时为 `0.000000`）与 `allocations`。可拆模式下
  `allocations` 按分配顺序、每条对应一个分组（`batch`、`expiry`（AI 17 原值
  YYMMDD）与 `weight`）；整包模式下**每条对应一个入选扫描**，按 FEFO 顺序返回。
  `scans` 为空数组时每个需求都是全额 `shortage`（`fulfilled` 为 `0.000000`，
  `allocations` 为空）。

请求示例（三个批次效期不同，需求 12 kg，目标单位 kg，`as_of` 之前无库存到期）：

```bash
curl -X POST http://localhost:8080/weight-pick-plan \
  -H "Content-Type: application/json" \
  -d '{"target_unit": "kg", "as_of": "2025-01-01", "scans": ["01095011015300031725033110LATE\u001d3103002000", "01095011015300031725013110EARLY\u001d3103002000", "01095011015300031725022810MID\u001d3202002513"], "demands": [{"gtin": "09501101530003", "quantity": 12}]}'
```

`EARLY`（2025-01-31，2 kg）最先到期全部分配，`MID`（2025-02-28，25.13 lb ≈
11.398776 kg）部分分配 10 kg，`LATE` 不动；响应 `200 OK`：

```json
{
  "target_unit": "kg",
  "demands": [
    {
      "gtin": "09501101530003",
      "quantity": 12.000000,
      "fulfilled": 12.000000,
      "shortage": 0.000000,
      "overage": 0.000000,
      "excluded_weight": 0.000000,
      "allocations": [
        {"batch": "EARLY", "expiry": "250131", "weight": 2.000000},
        {"batch": "MID",   "expiry": "250228", "weight": 10.000000}
      ]
    }
  ]
}
```

效期门槛示例：`as_of` 为 2025-02-10、`min_remaining_days` 为 10，门槛即
2025-02-20——`OLD`（2025-01-31 到期）被排除并计入 `excluded_weight`，
`FRESH`（2025-02-28 到期）合格并参与分配：

```bash
curl -X POST http://localhost:8080/weight-pick-plan \
  -H "Content-Type: application/json" \
  -d '{"as_of": "2025-02-10", "scans": ["01095011015300031725013110OLD\u001d3103002000", "01095011015300031725022810FRESH\u001d3103002000"], "demands": [{"gtin": "09501101530003", "quantity": 2, "min_remaining_days": 10}]}'
```

```json
{
  "target_unit": "kg",
  "demands": [
    {
      "gtin": "09501101530003",
      "quantity": 2.000000,
      "fulfilled": 2.000000,
      "shortage": 0.000000,
      "overage": 0.000000,
      "excluded_weight": 2.000000,
      "allocations": [
        {"batch": "FRESH", "expiry": "250228", "weight": 2.000000}
      ]
    }
  ]
}
```

库存不足时缺口计入 `shortage`（空 `scans` 即全额短缺）：

```bash
curl -X POST http://localhost:8080/weight-pick-plan \
  -H "Content-Type: application/json" \
  -d '{"as_of": "2025-01-15", "scans": [], "demands": [{"gtin": "09501101530003", "quantity": 2.5}]}'
```

```json
{"target_unit": "kg", "demands": [{"gtin": "09501101530003", "quantity": 2.500000, "fulfilled": 0.000000, "shortage": 2.500000, "overage": 0.000000, "excluded_weight": 0.000000, "allocations": []}]}
```

整包模式示例：三条扫描分别为 2 kg（`P1`，2025-01-31 到期）、3 kg（`P2`，
2025-02-28 到期）、4 kg（`P3`，2025-03-31 到期），需求 5 kg。达到 5 kg 的子集中
`{P1, P2}` 恰好 5 kg（超量 0），优于 `{P1, P3}`（超量 1）与 `{P2, P3}`（超量 2），
故选中 `P1`、`P2` 两个包裹，按 FEFO 顺序返回：

```bash
curl -X POST http://localhost:8080/weight-pick-plan \
  -H "Content-Type: application/json" \
  -d '{"as_of": "2025-01-01", "scans": ["01095011015300031725013110P1\u001d3103002000", "01095011015300031725022810P2\u001d3103003000", "01095011015300031725033110P3\u001d3103004000"], "demands": [{"gtin": "09501101530003", "quantity": 5, "package_mode": "whole"}]}'
```

```json
{
  "target_unit": "kg",
  "demands": [
    {
      "gtin": "09501101530003",
      "quantity": 5.000000,
      "fulfilled": 5.000000,
      "shortage": 0.000000,
      "overage": 0.000000,
      "excluded_weight": 0.000000,
      "allocations": [
        {"batch": "P1", "expiry": "250131", "weight": 2.000000},
        {"batch": "P2", "expiry": "250228", "weight": 3.000000}
      ]
    }
  ]
}
```

同一请求把 `quantity` 改为 `6.5` 时，没有恰好 6.5 kg 的子集，超量最小的是
`{P2, P3}`（7 kg，超量 0.5 kg）：响应中 `fulfilled` 为 `7.000000`，`overage` 为
`0.500000`，`shortage` 为 `0.000000`（不为负）。若需求超过全部合格包裹总重，则
选取全部合格包裹，缺口留在 `shortage`，`overage` 为 `0.000000`。

单个需求的合格包裹超过 30 个时返回 `422`：

```json
{
  "detail": {
    "demand_index": 0,
    "error": {
      "error": "TOO_MANY_PACKAGES",
      "message": "demand 0: 31 eligible packages for GTIN '09501101530003' exceed the whole-package limit of 30",
      "expected": "at most 30 eligible packages per whole-package demand",
      "found": "31"
    }
  }
}
```

扫描错误与 `/weight-summary` 同形（`detail.scan_index` + 原始错误）；需求中 GTIN
重复时返回 `422`：

```json
{
  "detail": {
    "demand_index": 1,
    "error": {
      "error": "DUPLICATE_GTIN",
      "message": "demand 1: GTIN '09501101530003' is already requested by demand 0; each GTIN may appear only once",
      "expected": "unique GTIN across all demands",
      "found": "09501101530003"
    }
  }
}
```

`min_remaining_days` 为负数时同样返回 `422`，`detail.demand_index` 定位出错需求：

```json
{
  "detail": {
    "demand_index": 1,
    "error": {
      "error": "INVALID_MIN_REMAINING_DAYS",
      "message": "demand 1: min_remaining_days must be a non-negative integer, got -3",
      "expected": "non-negative integer min_remaining_days",
      "found": "-3"
    }
  }
}
```

## 召回范围盘点 `POST /recall-impact`

一次提交多条扫码字符串和一组**有序召回规则**，按 **GTIN + 序列号（AI 01 + AI 21）**
识别实物，统计每条规则命中的包裹数与总净重，并汇总未命中任何规则的实物。

- 请求体：`{"scans": [...], "target_unit": "kg", "rules": [{"gtin": "...", "batches": ["B1"], "expiry_from": "250101", "expiry_to": "250228"}]}`。
  `target_unit` 可选，仅接受 `"kg"` / `"lb"`，默认 `"kg"`；`scans` 中的每一项既可以是
  元素串，也可以是 GS1 Digital Link URI（与 `/parse` 完全一致）。
- 每条扫描必须包含**恰好一个 AI 01**、**恰好一个 AI 17**（效期）、**恰好一个 AI 21**
  （序列号）、**零个或一个 AI 10**（批号可缺省）和**恰好一个重量 AI**（310n 或 320n，
  允许混合千克/磅）。任一条扫描解析失败或缺少/重复必需 AI，整个请求返回 `422`，
  `detail.scan_index` 给出出错下标（从 0 开始），`detail.error` 为该条扫描的原始错误
  （结构同 `/parse`）。
- 实物识别与去重：**GTIN + 序列号**唯一确定一个实物。同一实物被重复扫描且批号、
  效期、重量完全一致时**只计一次**（效期按 AI 17 原值比较；重量按单位相同且数值
  精确相等判断，例如 `3103 002513` 与 `3104 025130` 同为 2.513 kg）。若同一实物的
  两次扫描在**批号、效期或重量**上不一致，整个请求返回 `422`：`detail.scan_index`
  为当前（冲突）扫描下标，`detail.first_index` 为该实物首次出现的下标，
  `detail.error.error` 为 `CONFLICTING_BATCH` / `CONFLICTING_EXPIRY` /
  `CONFLICTING_WEIGHT`。
- 规则字段：`gtin` 必填（仅作匹配条件，不校验校验位）；`batches` 可选批号集合——
  省略时匹配任意批号，给定时实物的 AI 10 必须在集合内（集合中的 `null` 匹配无批号
  实物；空集合不匹配任何实物）；`expiry_from` / `expiry_to` 可选效期闭区间边界，
  格式为 6 位 YYMMDD（日 `00` 表示当月最后一天），按**实际日期**比较、两端皆含，
  可只给一端。边界不是 6 位数字、不是真实日期或 `expiry_from` 晚于 `expiry_to`
  时返回 `422`，`detail.rule_index` 定位出错规则（错误码 `INVALID_EXPIRY_BOUND` /
  `INVALID_DATE` / `EXPIRY_RANGE_INVERTED`）。规则校验先于扫描解析。
- 匹配：实物按规则**原序**归入**首条**命中的规则；未命中任何规则的实物计入
  `unmatched`。
- 响应：`target_unit`、与请求同序的 `rules` 数组和 `unmatched`。每条规则回显
  `rule_index`、`gtin`、`batches`、`expiry_from`、`expiry_to`，并给出 `package_count`
  （命中实物数）、`unit` 与 `total_weight`；`unmatched` 给出未命中实物的
  `package_count`、`unit` 与 `total_weight`。所有总重全程 `Decimal` 计算（换算系数
  **1 lb = 0.45359237 kg**），仅在响应时按**六位小数 half-even（银行家舍入）**输出
  为 JSON 数字（如 `2.513000`）。
- `scans` 为空时所有规则与 `unmatched` 均为 `0` / `0.000000`；`rules` 为空时全部
  实物计入 `unmatched`。

请求示例（`SN2` 被重复扫描且属性一致只计一次；`SN3` 批号不在集合内而未命中）：

```bash
curl -X POST http://localhost:8080/recall-impact \
  -H "Content-Type: application/json" \
  -d '{"target_unit": "kg", "scans": ["01095011015300031725013110B1\u001d21SN1\u001d3103002000", "01095011015300031725022810B2\u001d21SN2\u001d3103003000", "01095011015300031725022810B2\u001d21SN2\u001d3103003000", "01095011015300031725033110B9\u001d21SN3\u001d3202002513"], "rules": [{"gtin": "09501101530003", "batches": ["B1", "B2"], "expiry_from": "250101", "expiry_to": "250228"}]}'
```

响应 `200 OK`：规则命中 `SN1`（2 kg）与 `SN2`（3 kg）共 2 包 5 kg；`SN3`
（25.13 lb ≈ 11.398776 kg）未命中：

```json
{
  "target_unit": "kg",
  "rules": [
    {
      "rule_index": 0,
      "gtin": "09501101530003",
      "batches": ["B1", "B2"],
      "expiry_from": "250101",
      "expiry_to": "250228",
      "package_count": 2,
      "unit": "kg",
      "total_weight": 5.000000
    }
  ],
  "unmatched": {"package_count": 1, "unit": "kg", "total_weight": 11.398776}
}
```

同一实物两次扫描属性冲突时返回 `422`（当前下标 1 与首次下标 0 一并给出）：

```json
{
  "detail": {
    "scan_index": 1,
    "first_index": 0,
    "error": {
      "error": "CONFLICTING_BATCH",
      "message": "scan 1: item GTIN '09501101530003' serial 'SN1' was first scanned at index 0 with batch 'B1', but this scan has batch 'B2'",
      "expected": "batch 'B1' (scan 0)",
      "found": "batch 'B2'"
    }
  }
}
```

规则效期区间倒置时返回 `422`，`detail.rule_index` 定位出错规则：

```json
{
  "detail": {
    "rule_index": 0,
    "error": {
      "error": "EXPIRY_RANGE_INVERTED",
      "message": "rule 0: expiry_from '250331' (2025-03-31) is after expiry_to '250131' (2025-01-31)",
      "expected": "expiry_from on or before expiry_to",
      "found": "250331 > 250131"
    }
  }
}
```

## 追溯事件审计 `POST /trace-audit`

一次提交一组**按发生顺序排列**的追溯事件，按 **GTIN + 序列号（AI 01 + AI 21）**
识别实物，逐项执行 **receive → pick → ship** 状态机，记录每项事件前后的状态与异常，
并按最终状态汇总实物数量。

- 请求体：`{"events": [{"type": "receive", "scan": "..."}]}`。`events` 按事件发生
  顺序排列；`type` 仅接受 `"receive"` / `"pick"` / `"ship"`（其他值返回 `422`）；
  `scan` 既可以是元素串，也可以是 GS1 Digital Link URI（与 `/parse` 完全一致）。
- 每条扫描必须包含**恰好一个 AI 01**、**恰好一个 AI 21**（序列号）、**零个或一个
  AI 10**（批号可缺省）和**零个或一个 AI 17**（效期可缺省）；出现的重量 AI
  （310n/320n）会被忽略。任一条扫描解析失败或缺少/重复必需 AI，整个请求返回
  `422`，`detail.event_index` 给出出错事件下标（从 0 开始），`detail.error` 为该条
  扫描的原始错误（结构同 `/parse`）。
- 实物识别与一致性：**GTIN + 序列号**唯一确定一个实物。实物的批号（AI 10 原值，
  缺省也算一种取值）与效期（AI 17 原值，缺省同理）由它的**首个事件**确定；后续
  事件的批号或效期不一致时整个请求返回 `422`：`detail.event_index` 为当前（冲突）
  事件下标，`detail.first_index` 为该实物首个事件的下标，`detail.error.error` 为
  `CONFLICTING_BATCH` / `CONFLICTING_EXPIRY`。
- 状态机：每个实物沿 `none`（尚无已完成阶段）→ `received` → `picked` →
  `shipped` 推进，只有恰好命中下一阶段的事件才推进状态；其余事件**记录异常且
  不推进状态**，异常码如下：
  - `FIRST_EVENT_NOT_RECEIVE`：实物尚未 receive 就收到 pick/ship（首个事件不是
    receive）；
  - `DUPLICATE_STAGE`：重复刚完成的阶段（如第二次 receive、第二次 ship）；
  - `STAGE_REGRESSION`：回退到更早的阶段（如 pick 之后又 receive、ship 之后又
    pick）；
  - `SKIPPED_STAGE`：跳过未完成的阶段（如未 pick 直接 ship）。
- 响应：`events` 按输入顺序逐项给出 `event_index`、`type`、`gtin`、`serial`、
  `state_before`、`state_after` 与 `anomaly`（事件推进状态时为 `null`，否则为含
  `error` / `message` / `expected` / `found` 的异常对象）；`summary` 汇总各最终
  状态的实物数量（`none` / `received` / `picked` / `shipped` 四项恒在）。
- `events` 为空数组时返回 `200`，`events` 为空、`summary` 各项为 0。

请求示例（`SN1` 走完整流程；`SN2` 重复 receive 记录异常；`SN3` 未收货先拣货）：

```bash
curl -X POST http://localhost:8080/trace-audit \
  -H "Content-Type: application/json" \
  -d '{"events": [{"type": "receive", "scan": "01095011015300031725013110B1\u001d21SN1"}, {"type": "pick", "scan": "01095011015300031725013110B1\u001d21SN1"}, {"type": "ship", "scan": "01095011015300031725013110B1\u001d21SN1"}, {"type": "receive", "scan": "01095011015300031725022810B2\u001d21SN2"}, {"type": "receive", "scan": "01095011015300031725022810B2\u001d21SN2"}, {"type": "pick", "scan": "010950110153000321SN3"}]}'
```

响应 `200 OK`：前三项依次推进 `none → received → picked → shipped`；第 4 项
`SN2` 正常收货，第 5 项重复 receive 记录 `DUPLICATE_STAGE` 且状态停留
`received`；第 6 项 `SN3` 的首个事件是 pick，记录 `FIRST_EVENT_NOT_RECEIVE`，
状态停留 `none`：

```json
{
  "events": [
    {"event_index": 0, "type": "receive", "gtin": "09501101530003", "serial": "SN1", "state_before": "none",     "state_after": "received", "anomaly": null},
    {"event_index": 1, "type": "pick",    "gtin": "09501101530003", "serial": "SN1", "state_before": "received", "state_after": "picked",   "anomaly": null},
    {"event_index": 2, "type": "ship",    "gtin": "09501101530003", "serial": "SN1", "state_before": "picked",   "state_after": "shipped",  "anomaly": null},
    {"event_index": 3, "type": "receive", "gtin": "09501101530003", "serial": "SN2", "state_before": "none",     "state_after": "received", "anomaly": null},
    {"event_index": 4, "type": "receive", "gtin": "09501101530003", "serial": "SN2", "state_before": "received", "state_after": "received",
     "anomaly": {"error": "DUPLICATE_STAGE", "message": "event 4: item GTIN '09501101530003' serial 'SN2' is already 'received'; the 'receive' stage was already completed", "expected": "the next event 'pick'", "found": "'receive'"}},
    {"event_index": 5, "type": "pick",    "gtin": "09501101530003", "serial": "SN3", "state_before": "none",     "state_after": "none",
     "anomaly": {"error": "FIRST_EVENT_NOT_RECEIVE", "message": "event 5: item GTIN '09501101530003' serial 'SN3' has not been received, but got a 'pick' event; the first event of an item must be 'receive'", "expected": "the next event 'receive'", "found": "'pick'"}}
  ],
  "summary": {"none": 1, "received": 1, "picked": 0, "shipped": 1}
}
```

同一实物不同事件的批号或效期不一致时返回 `422`（当前下标与首次下标一并给出）：

```json
{
  "detail": {
    "event_index": 1,
    "first_index": 0,
    "error": {
      "error": "CONFLICTING_BATCH",
      "message": "event 1: item GTIN '09501101530003' serial 'SN1' was first seen at event 0 with batch 'B1', but this event has batch 'B2'",
      "expected": "batch 'B1' (event 0)",
      "found": "batch 'B2'"
    }
  }
}
```

扫描解析或结构错误与 `/weight-summary` 同形，`detail.event_index` 定位出错事件：

```json
{
  "detail": {
    "event_index": 0,
    "error": {
      "error": "MISSING_AI",
      "message": "event 0: missing required AI 21 (serial number)",
      "expected": "exactly one AI 01 (GTIN), exactly one AI 21 (serial number), at most one AI 10 (batch/lot) and at most one AI 17 (expiry date)"
    }
  }
}
```

## 生产批次谱系审计 `POST /batch-genealogy`

一次提交**期初批次库存**和一组**顺序任意**的转换工序，按 **GTIN + 批号（AI 01 + AI 10）**
标识批次：先做结构校验（一批一产、无来源批次），再由**实时库存动态调度**工序——每轮在全部
投入均被当前**未舍入**余量覆盖的工序中执行**请求下标最小**者，逐步扣减库存并记入产出，返回
工序谱系、各工序投入/产出/损耗重量与全部批次的最终余量。期初库存足以驱动 `A→B→A` 这类
闭环时（期初 A 先经工序变为 B、B 再变回 A），闭环可以成功执行。

- 请求体：`{"opening_stock": [...], "processes": [{"inputs": [...], "outputs": [...]}], "target_unit": "kg"}`。
  `target_unit` 可选，仅接受 `"kg"` / `"lb"`，默认 `"kg"`；`opening_stock` 为期初库存
  扫码数组；`processes` 为工序数组（顺序任意），每道工序含 `inputs`（消耗）与
  `outputs`（产出）两个扫码数组（字段必填，可为空数组）。所有扫码值既可以是元素串，
  也可以是 GS1 Digital Link URI（与 `/parse` 完全一致）。
- 每条扫码必须包含**恰好一个 AI 01**、**恰好一个 AI 10**（批号必填——批次由
  GTIN+批号标识）和**恰好一个重量 AI**（310n 或 320n，允许混合千克/磅）；AI 17/21
  会被忽略。任一条扫码解析失败或缺少/重复必需 AI，整个请求返回 `422`：期初库存出错时
  `detail.scan_index` 给出下标；工序扫码出错时 `detail.process_index` 给出工序下标，
  并以 `detail.input_index` / `detail.output_index` 给出工序内下标；`detail.error`
  为该条扫码的原始错误（结构同 `/parse`）。
- 重量一律使用 `Decimal` 从**原始六位数字与精度位 n** 重建，按固定系数
  **1 lb = 0.45359237 kg** 换算到 `target_unit` 后按 **GTIN+批号** 聚合：期初库存
  聚合一次，每道工序的投入、产出分别聚合。
- 结构校验（在扣减任何库存之前；出错均整单返回 `422` 并携带工序下标）：
  - **一批多产**：同一批次最多由一道工序产出。第二道产出该批次的工序使整单失败，
    `detail.process_index` 为当前工序、`detail.first_index` 为首次产出的工序
    （错误码 `MULTIPLE_PRODUCERS`）。
  - **无来源批次**：工序消耗的批次必须来自期初库存或某道工序的产出，否则整单失败，
    `detail.process_index` 定位出错工序（错误码 `UNKNOWN_BATCH`）。
- 库存驱动调度：每一轮在当前**未舍入 Decimal** 库存足以覆盖全部投入的工序中，执行
  **请求下标最小**者；投入不足、但对应批次的生产者工序尚未执行时继续等待（期初库存或
  更早的产出可以带着生产链穿过结构上的闭环）。无工序可执行时区分两种情况：
  - **循环依赖**：剩余工序仍在互相等待（每条不足投入的生产者都是另一道剩余工序）时整单
    失败，`detail.process_index` 与 `detail.cycle` 给出环上的工序下标（错误码
    `CYCLE_DETECTED`；工序消耗自身产出且无期初库存覆盖即为长度 1 的环）。
  - **库存不足**：某批次的来源均已执行完毕（或只来自期初库存）而余量仍不足时整单失败，
    `detail.process_index` 定位该工序、`error.message` 给出批次、需求量与可用量（错误码
    `INSUFFICIENT_STOCK`）。
- 库存扣减：每道可执行工序先按批次扣减投入、再记入产出。同一批次既在期初库存又由工序
  产出时，数量累加。
- 响应：`target_unit`、`processes` 与 `batches`。`processes` 按**实际执行顺序**排列，
  每项含 `process_index`（请求中的下标）、按批次聚合的 `inputs` / `outputs`（各含
  `gtin`、`batch`、`weight`，批次按工序内首次出现排序）以及 `input_weight` /
  `output_weight` / `loss_weight`（损耗 = 投入 − 产出，产出大于投入时为负）。
  `batches` 按首次出现顺序（期初库存在前，其后按请求顺序各工序的产出）给出全部批次的
  `opening` / `produced` / `consumed` / `remaining` 重量。所有重量全程 `Decimal`
  计算，仅在响应时按**六位小数 half-even（银行家舍入）**输出为 JSON 数字
  （如 `2.513000`）。
- `opening_stock` 与 `processes` 均为空数组时返回 `200` 与空的 `processes` / `batches`。

请求示例（10 kg 原料批次 `R1`；工序 1 消耗 6 kg `R1` 产出 5.5 kg 成品批次 `P1`，
工序 0 再消耗 2 kg `P1` 产出 1.9 kg 批次 `P2`——请求中工序顺序颠倒，仍按库存就绪先后执行）：

```bash
curl -X POST http://localhost:8080/batch-genealogy \
  -H "Content-Type: application/json" \
  -d '{"opening_stock": ["010950110153000310R1\u001d3103010000"], "processes": [{"inputs": ["010000000000001710P1\u001d3103002000"], "outputs": ["010000000000001710P2\u001d3103001900"]}, {"inputs": ["010950110153000310R1\u001d3103006000"], "outputs": ["010000000000001710P1\u001d3103005500"]}]}'
```

响应 `200 OK`：`processes` 按实际执行顺序排列（工序 1 先于工序 0），`batches` 给出
三个批次的最终余量（`R1` 余 4 kg、`P1` 余 3.5 kg、`P2` 余 1.9 kg）：

```json
{
  "target_unit": "kg",
  "processes": [
    {
      "process_index": 1,
      "inputs": [{"gtin": "09501101530003", "batch": "R1", "weight": 6.000000}],
      "outputs": [{"gtin": "00000000000017", "batch": "P1", "weight": 5.500000}],
      "input_weight": 6.000000,
      "output_weight": 5.500000,
      "loss_weight": 0.500000
    },
    {
      "process_index": 0,
      "inputs": [{"gtin": "00000000000017", "batch": "P1", "weight": 2.000000}],
      "outputs": [{"gtin": "00000000000017", "batch": "P2", "weight": 1.900000}],
      "input_weight": 2.000000,
      "output_weight": 1.900000,
      "loss_weight": 0.100000
    }
  ],
  "batches": [
    {"gtin": "09501101530003", "batch": "R1", "opening": 10.000000, "produced": 0.000000, "consumed": 6.000000, "remaining": 4.000000},
    {"gtin": "00000000000017", "batch": "P2", "opening": 0.000000, "produced": 1.900000, "consumed": 0.000000, "remaining": 1.900000},
    {"gtin": "00000000000017", "batch": "P1", "opening": 0.000000, "produced": 5.500000, "consumed": 2.000000, "remaining": 3.500000}
  ]
}
```

同一批次由两道工序产出时返回 `422`（当前工序下标与首次产出下标一并给出）：

```json
{
  "detail": {
    "process_index": 1,
    "first_index": 0,
    "error": {
      "error": "MULTIPLE_PRODUCERS",
      "message": "process 1: batch GTIN '00000000000017' batch 'P1' is already produced by process 0; each batch may be produced by at most one process",
      "expected": "batch produced only by process 0",
      "found": "second producer at process 1"
    }
  }
}
```

消耗无来源批次（既不在期初库存也不由任何工序产出）时返回 `422`：

```json
{
  "detail": {
    "process_index": 0,
    "error": {
      "error": "UNKNOWN_BATCH",
      "message": "process 0: input batch GTIN '09501101530003' batch 'ZZ' has no source: it is neither in the opening stock nor produced by any process",
      "expected": "input batch present in opening_stock or produced by exactly one process",
      "found": "GTIN '09501101530003' batch 'ZZ'"
    }
  }
}
```

期初库存足以驱动闭环时，结构上成环的生产链可以成功执行。例如下例两道工序构成
`A→B→A`，但期初有 10 kg 的 `A`：工序 0 先消耗 10 kg `A` 产出 8 kg `B`，工序 1
再消耗 8 kg `B` 产出 9 kg `A`，最终 `A` 余 9 kg、`B` 余 0 kg，`processes` 按
实际执行顺序 `[0, 1]` 返回（期初库存不足以启动闭环时，例如只有 5 kg `A` 而工序
需要 10 kg，则两道工序仍互相等待，按 `CYCLE_DETECTED` 拒绝）：

```bash
curl -X POST http://localhost:8080/batch-genealogy \
  -H "Content-Type: application/json" \
  -d '{"opening_stock": ["010950110153000310A\u001d3103010000"], "processes": [{"inputs": ["010950110153000310A\u001d3103010000"], "outputs": ["010950110153000310B\u001d3103008000"]}, {"inputs": ["010950110153000310B\u001d3103008000"], "outputs": ["010950110153000310A\u001d3103009000"]}]}'
```

```json
{
  "target_unit": "kg",
  "processes": [
    {
      "process_index": 0,
      "inputs": [{"gtin": "09501101530003", "batch": "A", "weight": 10.000000}],
      "outputs": [{"gtin": "09501101530003", "batch": "B", "weight": 8.000000}],
      "input_weight": 10.000000,
      "output_weight": 8.000000,
      "loss_weight": 2.000000
    },
    {
      "process_index": 1,
      "inputs": [{"gtin": "09501101530003", "batch": "B", "weight": 8.000000}],
      "outputs": [{"gtin": "09501101530003", "batch": "A", "weight": 9.000000}],
      "input_weight": 8.000000,
      "output_weight": 9.000000,
      "loss_weight": -1.000000
    }
  ],
  "batches": [
    {"gtin": "09501101530003", "batch": "A", "opening": 10.000000, "produced": 9.000000, "consumed": 10.000000, "remaining": 9.000000},
    {"gtin": "09501101530003", "batch": "B", "opening": 0.000000, "produced": 8.000000, "consumed": 8.000000, "remaining": 0.000000}
  ]
}
```

无期初库存覆盖、剩余工序仍互相等待时才按循环依赖拒绝（下例工序 0 消耗 `B` 产出
`A`、工序 1 消耗 `A` 产出 `B`），`cycle` 给出环上的工序下标：

```json
{
  "detail": {
    "process_index": 0,
    "cycle": [0, 1],
    "error": {
      "error": "CYCLE_DETECTED",
      "message": "processes 0 -> 1 -> 0 form a production/consumption dependency cycle; the graph must be acyclic",
      "expected": "an acyclic producer/consumer dependency graph",
      "found": "cycle 0 -> 1 -> 0"
    }
  }
}
```

某道工序超耗（投入超过当前可用余量，且对应批次的生产者均已执行）时返回 `422`，
需求量与可用量一并给出：

```json
{
  "detail": {
    "process_index": 0,
    "error": {
      "error": "INSUFFICIENT_STOCK",
      "message": "process 0: batch GTIN '09501101530003' batch 'R1' requires 6.000000 kg but only 4.000000 kg is available",
      "expected": "at most 4.000000 kg of batch GTIN '09501101530003' batch 'R1'",
      "found": "6.000000 kg"
    }
  }
}
```

## 装箱装载审计 `POST /packing-audit`

一次提交一组**顺序任意**的容器（托盘、周转箱等），容器可通过 `parent_id` 相互嵌套；
按 **GTIN + 序列号（AI 01 + AI 21）** 识别每件货品，按**叶到根**汇总每个容器的
直属货品重量与子容器重量，标记超重容器。

- 请求体：`{"target_unit": "kg", "containers": [{"container_id": "PALLET-1", "parent_id": null, "max_weight": 20, "items": ["..."]}]}`。
  `target_unit` 可选，仅接受 `"kg"` / `"lb"`，默认 `"kg"`；`containers` 为容器数组，
  顺序任意（子容器可以排在父容器之前），**响应按请求顺序返回**。`items` 中的每一项
  既可以是元素串，也可以是 GS1 Digital Link URI（与 `/parse` 完全一致）。
- 每个容器含：必填的 `container_id`（非空字符串，**全请求唯一**）、可选的 `parent_id`
  （省略或 `null` 表示根容器；给定时必须等于本请求中某个 `container_id`）、必填的
  `max_weight`（容量上限，以 `target_unit` 计，**非负 `Decimal`**，JSON number 或字符串
  均可）和必填的 `items`（直属货品扫码数组，可为空）。
- **`max_weight` 位数边界**：容量最多 **61 位整数位**（即小于 `1e61`，`1e60` 仍合法）
  且最多 **50 位小数位**——这与内部 Decimal 工作精度及六位小数 JSON 输出能力一致，防止
  超大整数量级与超深小数精度相加扩张运算上下文（单请求长期占用 CPU/内存）。越界时整单
  返回 `422 CAPACITY_OUT_OF_RANGE`，`detail.container_index` 定位出错容器；边界内
  （如 `1e60`）仍返回完整审计结果。
- 每条货品扫码必须包含**恰好一个 AI 01**、**恰好一个 AI 21**（序列号）和**恰好一个
  重量 AI**（310n 或 320n，允许混合千克/磅）；AI 10（批号）与 AI 17（效期）可有可无、
  至多一个，不参与审计。任一条扫码解析失败或缺少/重复必需 AI，整个请求返回 `422`，
  `detail.container_index` 给出容器下标、`detail.item_index` 给出容器内货品下标（均从
  0 开始），`detail.error` 为该条扫码的原始错误（结构同 `/parse`）。
- 容器关系必须构成**森林（一棵或多棵有根树）**，以下结构错误均整单返回 `422` 并定位
  容器下标：
  - **重复容器**（`DUPLICATE_CONTAINER`）：`container_id` 重复时，`container_index`
    为当前下标、`first_container_index` 为首次出现下标；
  - **未知父容器**（`UNKNOWN_PARENT`）：`parent_id` 指向本请求中不存在的容器，
    `container_index` 定位出错容器；
  - **环**（`CYCLE_DETECTED`）：父指针不得成环（容器以自身为父也是长度 1 的环），
    `container_index` 与 `cycle` 给出环上的容器下标（`cycle` 已旋转至最小下标在前）。
- **同一货品不得重复装载**：同一 **GTIN + 序列号** 在所有容器的全部 `items` 中只能
  出现一次（同一容器内重复也算），否则整单 `422`（`DUPLICATE_ITEM`）：
  `container_index` / `item_index` 为第二次出现位置，`first_container_index` /
  `first_item_index` 为首次出现位置。
- 重量一律使用 `Decimal` 从**原始六位数字与精度位 n** 重建，按固定系数
  **1 lb = 0.45359237 kg** 换算到 `target_unit`。汇总按**叶到根**进行：每个容器的
  `direct_weight` 为其直属货品重量之和，`total_weight` = 直属货品重量 + 全部子容器的
  `total_weight`；`recursive_item_count` 同理递归统计货品数。
- **超重不报错**：某容器的（未舍入的精确）`total_weight` 超过 `max_weight` 时仍返回
  完整审计结果，仅将该容器标记为 `"over_limit": true`，其 `remaining_capacity`
  （= `max_weight - total_weight`，在与位数边界一致的**固定受限精度**下计算）为负值；
  恰好等于上限不算超重。超重判定使用舍入前两操作数的**精确比较**，与六位响应值分离，
  因此六位 half-even 舍入显示为 `0.000000` 时仍可正确判超。所有重量仅在响应时按**六位
  小数 half-even（银行家舍入）**输出为 JSON 数字（如 `2.513000`）。
- 响应：`target_unit` 与**按请求顺序**排列的 `containers` 数组，每项含 `container_id`、
  `parent_id`（回显）、`max_weight`、`unit`、`direct_weight`、`total_weight`、
  `remaining_capacity`、`direct_item_count`（直属货品数）、`recursive_item_count`
  （直属 + 全部子容器内货品数）与 `over_limit`。
- `containers` 为空数组时返回 `200` 与空的 `containers`。

请求示例（托盘 `PALLET-1` 限重 20 kg，直属 6 kg；箱 `BOX-A` 装入 2 kg 与 25.13 lb
两件货并作为其子容器；箱 `BOX-B` 再装入 `BOX-A`，直属 1 kg）：

```bash
curl -X POST http://localhost:8080/packing-audit \
  -H "Content-Type: application/json" \
  -d '{"target_unit": "kg", "containers": [{"container_id": "PALLET-1", "max_weight": 20, "items": ["010950110153000321P1\u001d3103006000"]}, {"container_id": "BOX-A", "parent_id": "PALLET-1", "max_weight": 5, "items": ["010950110153000321A1\u001d3103002000", "010950110153000321A2\u001d3202002513"]}, {"container_id": "BOX-B", "parent_id": "BOX-A", "max_weight": 100, "items": ["010000000000001721B1\u001d3103001000"]}]}'
```

`BOX-B` 总重 1 kg；`BOX-A` 直属 `2 + 25.13 × 0.45359237 = 13.3987762581 kg`、
加子箱共 14.398776 kg，超过 5 kg 上限；托盘含直属 6 kg 与两个子箱共 20.398776 kg，
同样超重（`remaining_capacity` 为负），但响应 `200 OK` 且结果完整：

```json
{
  "target_unit": "kg",
  "containers": [
    {"container_id": "PALLET-1", "parent_id": null,   "max_weight": 20.000000, "unit": "kg", "direct_weight": 6.000000,  "total_weight": 20.398776, "remaining_capacity": -0.398776, "direct_item_count": 1, "recursive_item_count": 4, "over_limit": true},
    {"container_id": "BOX-A",    "parent_id": "PALLET-1", "max_weight": 5.000000, "unit": "kg", "direct_weight": 13.398776, "total_weight": 14.398776, "remaining_capacity": -9.398776, "direct_item_count": 2, "recursive_item_count": 3, "over_limit": true},
    {"container_id": "BOX-B",    "parent_id": "BOX-A", "max_weight": 100.000000, "unit": "kg", "direct_weight": 1.000000,  "total_weight": 1.000000,  "remaining_capacity": 99.000000, "direct_item_count": 1, "recursive_item_count": 1, "over_limit": false}
  ]
}
```

同一货品被重复装载时返回 `422`，两次出现的容器/货品下标一并给出：

```json
{
  "detail": {
    "container_index": 1,
    "item_index": 0,
    "first_container_index": 0,
    "first_item_index": 0,
    "error": {
      "error": "DUPLICATE_ITEM",
      "message": "container 1 item 0: item GTIN '09501101530003' serial 'SN1' was already loaded at container 0 item 0; an item may be loaded only once",
      "expected": "item first seen at container 0 item 0 to be the only occurrence",
      "found": "GTIN '09501101530003' serial 'SN1' at container 1 item 0"
    }
  }
}
```

父容器不存在、容器关系成环、容器 id 重复时同样返回 `422`，下例为父子成环
（`cycle` 给出环上下标，已旋转至最小下标在前）：

```json
{
  "detail": {
    "container_index": 0,
    "cycle": [0, 1],
    "error": {
      "error": "CYCLE_DETECTED",
      "message": "containers 0 -> 1 -> 0 form a parent/child cycle; the container relationships must form a forest",
      "expected": "an acyclic container forest",
      "found": "cycle 0 -> 1 -> 0"
    }
  }
}
```

`max_weight` 超出位数边界（多于 61 位整数位或多于 50 位小数位，含 `1E999999` 这类
指数炸弹）时返回 `422 CAPACITY_OUT_OF_RANGE`，由 `container_index` 定位：

```json
{
  "detail": {
    "container_index": 0,
    "error": {
      "error": "CAPACITY_OUT_OF_RANGE",
      "message": "container 0: max_weight is outside the supported capacity envelope of at most 61 integer digits and 50 decimal places",
      "expected": "a finite non-negative Decimal with at most 61 integer digits and at most 50 decimal places",
      "found": "1000000 integer digits and 0 decimal places"
    }
  }
}
```

扫码解析或结构错误与 `/batch-genealogy` 同形，`detail.container_index` 与
`detail.item_index` 定位出错货品：

```json
{
  "detail": {
    "container_index": 0,
    "item_index": 1,
    "error": {
      "error": "MISSING_AI",
      "message": "item 1: missing required AI 21 (serial number)",
      "expected": "exactly one AI 01 (GTIN), exactly one AI 21 (serial number), and exactly one weight AI 310n/320n"
    }
  }
}
```

## 批次重量双重抽样验收 `POST /weight-sampling-audit`

一次提交一组按 **GTIN + 批号（AI 01 + AI 10）** 定义的验收计划与一组按检验顺序排列的
抽样样本，执行**双重抽样（double sampling）**判定：每件样本的换算净重落在计划声明的
**重量闭区间** `[min_weight, max_weight]` 之外（严格小于下限或严格大于上限）计为一个缺陷，
区间边界本身合格。

- 请求体：`{"target_unit": "kg", "plans": [{...}], "samples": [{"stage": 1, "scan": "..."}]}`。
  `target_unit` 可选，仅接受 `"kg"` / `"lb"`，默认 `"kg"`；区间上下限与全部重量比较均以
  该单位计。
- 每个计划（`plans` 数组）含：`gtin`、`batch`（二者组合**全请求唯一**，重复返回 `422`
  `DUPLICATE_PLAN`，给出 `plan_index` 与 `first_plan_index`）、`min_weight` /
  `max_weight`（闭区间上下限，**Decimal**，JSON number 或字符串均可；下限不得大于上限，
  否则 `422 INVALID_INTERVAL` 并定位 `plan_index`）以及双重抽样参数：
  - `n1` / `a1` / `r1`：第一阶段样本量、接收数、拒收数；
  - `n2` / `a2` / `r2`：两阶段**累计**样本量、累计接收数、累计拒收数。
  必须满足 **`n2 > n1`** 且 **`0 ≤ a1 < r1 ≤ n1`**、**`0 ≤ a2 < r2 ≤ n2`**，任一关系
  不成立均返回 `422 INVALID_THRESHOLDS`，`detail.plan_index` 定位出错计划，`found`
  回显六个参数。
- 每个样本（`samples` 数组）含 `stage`（只能是 `1` 或 `2`）与 `scan`（元素串或
  GS1 Digital Link URI，规则同 `/parse`）。每条扫码必须包含**恰好一个 AI 01**、
  **恰好一个 AI 10**、**恰好一个 AI 21** 与**恰好一个重量 AI**（310n/320n，千克/磅
  可混用）；AI 17 可有可无、至多一个，不参与判定。解析失败或缺/重必需 AI 时整单
  `422`，`detail.sample_index` 定位出错样本、`detail.error` 为原始扫码错误。
- 样本结构错误（均为 `422` 并定位索引）：
  - **同一 GTIN+序列号不得重复**（`DUPLICATE_ITEM`）：`sample_index` 为第二次出现
    位置、`first_sample_index` 为首次位置（跨阶段重复也算）；
  - **无匹配计划**（`NO_MATCHING_PLAN`）：样本的 GTIN+批号在 `plans` 中找不到同键
    计划，`sample_index` 定位样本；
  - **阶段样本超额**（`STAGE_OVERFLOW`）：同一计划最多 `n1` 个一阶段样本、
    `n2 - n1` 个二阶段样本（两阶段合计不超过 `n2`），超出的样本触发 `422`，
    `sample_index` 定位该样本、`plan_index` 定位所属计划；
  - **定论后再抽二阶段**（`STAGE_AFTER_DECISION`）：一阶段抽满 `n1` 件且缺陷数
    已 `≤ a1`（接收）或 `≥ r1`（拒收）后，该计划即**终局定论、抽样终止**；此后
    任何仍标记 `stage=2` 的样本一律立即 `422` 拒绝——**不计入** `stage2_count` /
    `total_count` 等任何计数，也不改变响应中的判定。错误给出 `sample_index`、
    `plan_index`、`expected_stage`（`1`）、`found_stage`（`2`）与定论时的
    `decision`（`"accept"` / `"reject"`）。注意：一阶段**未满** `n1`，或虽抽满但
    缺陷数严格介于 `a1` 与 `r1` 之间（未定论）时，二阶段样本仍是合法的继续抽样。
- 判定规则（每个计划独立；一阶段结论一旦成立即为**最终结论，二阶段不得改写**）：
  1. 一阶段抽满 `n1` 件后：缺陷数 `≤ a1` → **接收（accept）**；`≥ r1` → **拒收
     （reject）**；介于两者之间（严格大于 `a1` 且严格小于 `r1`）则进入二阶段。
     接收/拒收一旦成立即为该计划的**终局**，抽样立即终止，其后任何 `stage=2`
     样本返回 `422 STAGE_AFTER_DECISION` 且不计数（见上）；
  2. 仅当两阶段**累计抽满 `n2` 件**时，才按累计缺陷数对照 `a2/r2`：`≤ a2` 接收、
     `≥ r2` 拒收；累计数仍严格介于 `a2` 与 `r2` 之间时保持 `pending`；
  3. 一阶段未满 `n1`，或一阶段未定论且二阶段累计未满 `n2`，决定均为
     **待定（pending）**。
- 重量一律使用 `Decimal` 从**原始六位数字与精度位 n** 重建，按固定系数
  **1 lb = 0.45359237 kg** 换算到 `target_unit` 后再与区间比较；比较全程不使用
  float。
- 响应：`target_unit` 与**按请求顺序**排列的 `plans` 数组，每项回显计划键值、区间、
  六个抽样参数，以及 `stage1_count` / `stage1_defects`（一阶段样本数与缺陷数）、
  `stage2_count` / `stage2_defects`（二阶段）、`total_count` / `total_defects`
  （两阶段累计）、`out_of_range_samples`（越界样本在 `samples` 数组中的下标，按检验
  顺序排列）与 `decision`（`"accept"` / `"reject"` / `"pending"`）。区间上下限按
  **六位小数 half-even** 输出为 JSON 数字。`plans` 与 `samples` 均为空时返回 `200`
  与空数组。

请求示例（计划：区间 `[1.9, 2.1] kg`，`n1=5, a1=0, r1=2`，`n2=10, a2=1, r2=2`；
一阶段 5 件中 SN04 重 2.5 kg 越界，缺陷数 1 介于 0 与 2 之间进入二阶段；二阶段 5 件
全部合格，累计 1 个缺陷 `≤ a2`，接收）：

```bash
curl -X POST http://localhost:8080/weight-sampling-audit \
  -H "Content-Type: application/json" \
  -d '{"target_unit": "kg", "plans": [{"gtin": "09501101530003", "batch": "B1", "min_weight": 1.9, "max_weight": 2.1, "n1": 5, "a1": 0, "r1": 2, "n2": 10, "a2": 1, "r2": 2}], "samples": [{"stage": 1, "scan": "010950110153000310B1\u001d21SN01\u001d3103002000"}, {"stage": 1, "scan": "010950110153000310B1\u001d21SN02\u001d3103002000"}, {"stage": 1, "scan": "010950110153000310B1\u001d21SN03\u001d3103001900"}, {"stage": 1, "scan": "010950110153000310B1\u001d21SN04\u001d3103002500"}, {"stage": 1, "scan": "010950110153000310B1\u001d21SN05\u001d3103002100"}, {"stage": 2, "scan": "010950110153000310B1\u001d21SN06\u001d3103002000"}, {"stage": 2, "scan": "010950110153000310B1\u001d21SN07\u001d3103002000"}, {"stage": 2, "scan": "010950110153000310B1\u001d21SN08\u001d3103002000"}, {"stage": 2, "scan": "010950110153000310B1\u001d21SN09\u001d3103002000"}, {"stage": 2, "scan": "010950110153000310B1\u001d21SN10\u001d3103002000"}]}'
```

注意 SN03 恰为下限 1.900 kg、SN05 恰为上限 2.100 kg，闭区间边界均判合格；SN04
（2.500 kg）是唯一缺陷。响应 `200 OK`：

```json
{
  "target_unit": "kg",
  "plans": [
    {
      "plan_index": 0,
      "gtin": "09501101530003",
      "batch": "B1",
      "min_weight": 1.900000,
      "max_weight": 2.100000,
      "unit": "kg",
      "n1": 5, "a1": 0, "r1": 2, "n2": 10, "a2": 1, "r2": 2,
      "stage1_count": 5,
      "stage1_defects": 1,
      "stage2_count": 5,
      "stage2_defects": 0,
      "total_count": 10,
      "total_defects": 1,
      "out_of_range_samples": [3],
      "decision": "accept"
    }
  ]
}
```

一阶段抽满即定论时抽样终止：例如一阶段 5 件中 0 个缺陷已接收，之后再提交
`stage=2` 样本（即使全部越界）会被**整单拒绝**，返回 `422 STAGE_AFTER_DECISION`，
这些样本不计入 `stage2_count` / `total_count`，合法前缀（5 件一阶段）本身的响应
仍为 `decision: "accept"`、二阶段各项为零。一阶段拒收同理。一阶段未满 `n1`、或
一阶段未定论而累计未满 `n2` 时 `decision` 为 `"pending"`；抽满 `n2` 但累计缺陷数
仍严格介于 `a2` 与 `r2` 之间（如 `a2=1, r2=3` 时累计 2 个缺陷）同样保持
`"pending"`。

定论后再抽二阶段返回 `422`（下例为一阶段 5 件全部合格、计划已接收后，第 6 个
样本仍标记 `stage=2`；`sample_index=5`、`plan_index=0`，并给出期望/实际阶段）：

```bash
curl -X POST http://localhost:8080/weight-sampling-audit \
  -H "Content-Type: application/json" \
  -d '{"target_unit": "kg", "plans": [{"gtin": "09501101530003", "batch": "B1", "min_weight": 1.9, "max_weight": 2.1, "n1": 5, "a1": 0, "r1": 2, "n2": 10, "a2": 1, "r2": 2}], "samples": [{"stage": 1, "scan": "010950110153000310B1\u001d21SN01\u001d3103002000"}, {"stage": 1, "scan": "010950110153000310B1\u001d21SN02\u001d3103002000"}, {"stage": 1, "scan": "010950110153000310B1\u001d21SN03\u001d3103002000"}, {"stage": 1, "scan": "010950110153000310B1\u001d21SN04\u001d3103002000"}, {"stage": 1, "scan": "010950110153000310B1\u001d21SN05\u001d3103002000"}, {"stage": 2, "scan": "010950110153000310B1\u001d21SN06\u001d3103002500"}]}'
```

```json
{
  "detail": {
    "sample_index": 5,
    "plan_index": 0,
    "error": {
      "error": "STAGE_AFTER_DECISION",
      "message": "sample 5: plan 0 (GTIN '09501101530003' batch 'B1') already reached a final stage-1 decision of 'accept' after 5 samples; no further samples may be drawn for a decided plan",
      "expected": "no further samples after a final stage-1 decision",
      "expected_stage": 1,
      "found_stage": 2,
      "decision": "accept"
    }
  }
}
```

若一阶段只有 1 个缺陷（严格介于 `a1=0` 与 `r1=2` 之间、未定论），同样的二阶段
样本则是**合法的继续抽样**：请求随其余二阶段样本正常返回 `200`，不会产生该错误。

阶段样本超额返回 `422`，定位到越额样本与其所属计划（下例为第 6 个一阶段样本，
一阶段容量为 5）：

```json
{
  "detail": {
    "sample_index": 5,
    "plan_index": 0,
    "error": {
      "error": "STAGE_OVERFLOW",
      "message": "sample 5: plan 0 (GTIN '09501101530003' batch 'B1') already holds 5 stage-1 samples, which is its stage-1 capacity",
      "expected": "at most 5 stage-1 sample(s) for this plan",
      "found": "stage 1 sample number 6"
    }
  }
}
```

非法阈值、无匹配计划、重复 GTIN+序列号同样返回 `422`；非法阈值定位计划下标，其余
定位样本下标：

```json
{
  "detail": {
    "plan_index": 0,
    "error": {
      "error": "INVALID_THRESHOLDS",
      "message": "plan 0: n2 must be greater than n1",
      "expected": "0 <= a1 < r1 <= n1, 0 <= a2 < r2 <= n2 and n2 > n1",
      "found": {"n1": 5, "a1": 0, "r1": 2, "n2": 5, "a2": 1, "r2": 2}
    }
  }
}
```

```json
{
  "detail": {
    "sample_index": 0,
    "error": {
      "error": "NO_MATCHING_PLAN",
      "message": "sample 0: no plan matches GTIN '09501101530003' batch 'B2'",
      "expected": "a plan with the same gtin and batch",
      "found": "GTIN '09501101530003' batch 'B2'"
    }
  }
}
```

## 错误响应

解析或校验失败返回 `422 Unprocessable Entity`，`detail` 中包含错误码、
字节偏移（`offset`，UTF-8 字节序）与预期规则（`expected`）：

```bash
curl -X POST http://localhost:8080/parse \
  -H "Content-Type: application/json" \
  -d '{"data": "0109501101530004"}'
```

```json
{
  "detail": {
    "error": "INVALID_CHECK_DIGIT",
    "message": "GTIN check digit mismatch: expected 3, found 4",
    "offset": 15,
    "expected": "Mod-10 check digit 3",
    "found": "4"
  }
}
```

错误码一览：

| 错误码                 | 说明                                                          |
| ---------------------- | ------------------------------------------------------------- |
| `EMPTY_INPUT`          | 输入为空（元素串为空，或 URI 路径/查询中没有任何 AI/值对）    |
| `UNEXPECTED_SEPARATOR` | 在应出现 AI 的位置遇到分隔符（仅元素串）                      |
| `INVALID_AI`           | 不是合法的数字 AI（元素串为非 2 位数字；URI 为非数字段/键）   |
| `UNSUPPORTED_AI`       | 不支持的 AI（仅支持 01/10/17/21 及 310n/320n）                |
| `INVALID_PRECISION`    | 310/320 前缀后缺少或给出非数字的精度位 `n`（须为 0-9）        |
| `FIELD_TOO_SHORT`      | 固定长度字段字符不足（含重量字段不足 6 位数字）               |
| `FIELD_EMPTY`          | 可变长度字段为空                                              |
| `FIELD_TOO_LONG`       | 可变长度字段超过 20 字符（或固定长度字段超长，仅 URI）        |
| `INVALID_CHARACTER`    | 字符不符合该字段字符集                                        |
| `INVALID_CHECK_DIGIT`  | GTIN Mod-10 校验位不匹配                                      |
| `INVALID_DATE`         | 效期不是真实日期                                              |
| `UNPAIRED_PATH`        | Digital Link URI 路径段不是成对的 AI/值（仅 URI）             |
| `DUPLICATE_AI`         | AI 重复出现，或同时出现在路径与查询参数中（仅 URI）           |
| `INVALID_PERCENT_ENCODING` | `%XX` 不是合法十六进制转义或不是合法 UTF-8（仅 URI）      |

## 本地开发

```bash
pip install -r requirements-dev.txt
pytest                 # 运行测试
uvicorn app.main:app --reload --port 8080
```

## 项目结构

```
app/
  main.py     # FastAPI 应用与请求/响应模型（/parse、/weight-summary、/weight-pick-plan、/recall-impact、/trace-audit、/batch-genealogy、/packing-audit、/weight-sampling-audit）
  gs1.py      # GS1 元素串 / Digital Link URI 解析与校验核心：不可变声明式 AI 注册表（精确 AI + 带精度位参数的 310n/320n 族）、两载体共享的最长匹配识别器与值校验分派
  summary.py  # 多扫描重量汇总：Decimal 重建、单位换算、GTIN+批号分组
  pickplan.py # FEFO 拣货计划：GTIN+批号+效期汇总、as_of+min_remaining_days 效期门槛、按实际效期分配需求；whole 整包模式按单条扫描做最小超量子集选择
  recall.py   # 召回范围盘点：GTIN+序列号实物去重与冲突检测、有序规则（GTIN+批号集合+效期闭区间）首命中归组、未命中汇总
  traceaudit.py # 追溯事件审计：GTIN+序列号实物识别、批号/效期一致性校验、receive→pick→ship 状态机与异常记录、最终状态汇总
  genealogy.py # 生产批次谱系审计：GTIN+批号批次台账、一批一产/无来源结构校验、未舍入库存动态调度（最小就绪下标执行、互待成环/超耗拒绝）、批次最终余量
  packing.py  # 装箱装载审计：容器森林校验（重复容器/未知父容器/环）、GTIN+序列号重复装载检测、叶到根直属与递归重量/货品数汇总、over_limit 超重标记
  sampling.py # 批次重量双重抽样验收：GTIN+批号计划（闭区间 + n1/a1/r1/n2/a2/r2 阈值校验）、样本唯一 01/10/21/重量 AI 校验与 GTIN+序列号去重、阶段容量、匹配计划与定论后终止（STAGE_AFTER_DECISION）校验、一阶段定论终局不可改写的 accept/reject/pending 判定
tests/
  test_parse.py
  test_ai_registry.py       # 不可变注册表：启动期声明校验、只读性与共享识别器（完整键/最长匹配）
  test_carrier_consistency.py # 元素串与 Digital Link 双载体：同值同元素、同错同码
  test_weight_summary.py
  test_weight_pick_plan.py
  test_recall_impact.py
  test_trace_audit.py
  test_batch_genealogy.py
  test_packing_audit.py
  test_weight_sampling_audit.py
Dockerfile
docker-compose.yml
requirements.txt      # 运行依赖
requirements-dev.txt  # 测试依赖
```
