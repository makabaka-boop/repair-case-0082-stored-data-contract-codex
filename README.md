# 潮闸检修航程调度（Tide Gate Scheduling）

潮闸检修后，闸门只在断续的开放窗口可用。本服务根据调度员发布的闸门日历，
在出发搜索区间内求出**全部可行出发时刻的合并区间**，并在调度员选定时刻后
生成逐闸「最早可入」见证、落库为可重放的采纳方案。

- 后端：Python 3.12 + FastAPI
- 数据库：PostgreSQL（不可变日历 + 采纳方案）
- 部署：Docker Compose（API + 数据库），宿主机端口由 `API_PORT` 配置
- 测试：`./verify` 执行 pytest（算法穷举一致性 + API + 二十万窗口 ≤ 3 秒）

## 规则要点

- 闸门 ID 为不重复的非空白 ASCII 字符串（长度 1..128）。
- 所有时刻与时长均为 `[0, 10^12]` 内的整数秒。
- 每个窗口满足 `start < end`，按 `start` 升序、互不重叠但可相接；
  窗口与搜索区间一律**左闭右开**，恰在 `end` 入闸判为关闭。
- 航程含 1..200 个**互异**闸门、相邻航行时长、每闸最大等待时长、出发搜索区间。
- 首闸到达即出发；下一闸到达 = 本闸入闸 + 相邻航行时长。
- 最早可入原则：到达落在窗口内立即入闸，否则等待下一窗口；
  等待时长（入闸 − 到达）不得超过该闸最大等待。
- 无可行出发时刻时返回空区间列表。
- 日历与方案不可变；非法日历整版拒绝（HTTP 422）、不落库。

算法自末闸起逆向传播「可行入闸时刻区间集」，每闸一次线性双指针扫描，
总体复杂度 O(总窗口数 + 闸数)；小规模结果与逐秒穷举逐一对照。

## 启动

```bash
# 默认宿主端口 8000；可用 API_PORT 改端口
API_PORT=8080 docker compose up --build
```

健康检查：`GET /health` → `{"status":"ok"}`

交互式 API 文档：`http://localhost:8000/docs`

## 测试

```bash
./verify          # 需要本机 Python 3 且已安装 requirements-dev.txt
# 或
DATABASE_URL="sqlite://" python3 -m pytest -q
```

## 请求示例

### 1. 发布日历 `POST /calendars`（201）

```json
{
  "gates": [
    {"gate_id": "G1", "windows": [[10, 20], [30, 40]]},
    {"gate_id": "G2", "windows": [[25, 35]]}
  ]
}
```

响应返回 `id`（日历 ID）。窗口重叠、`start >= end`、ID 重复或含非 ASCII
字符都会返回 422 且不写入任何数据。`GET /calendars/{id}` 可读回。

### 2. 探测可行出发区间 `POST /voyages/probe`（200）

```json
{
  "calendar_id": "<calendar_id>",
  "gates": ["G1", "G2"],
  "legs": [5],
  "max_waits": [5, 5],
  "search_start": 0,
  "search_end": 50
}
```

- `legs` 长度 = 闸门数 − 1；`max_waits` 长度 = 闸门数。
- 响应：`{"intervals": [[15, 20]]}`，区间为左闭右开整数区间，已合并相接/重叠者。
- 无解：`{"intervals": []}`。
- 航程引用日历中不存在的闸门：422；日历不存在：404。

### 3. 选定时刻并采纳 `POST /plans`（201 / 409）

```json
{
  "calendar_id": "<calendar_id>",
  "gates": ["G1", "G2"],
  "legs": [5],
  "max_waits": [5, 5],
  "departure": 15
}
```

成功返回方案 `id` 与逐闸见证（最早可入原则生成）：

```json
{
  "id": "<plan_id>",
  "departure": 15,
  "witnesses": [
    {"gate_id": "G1", "arrival": 15, "entry": 15, "wait": 0},
    {"gate_id": "G2", "arrival": 20, "entry": 25, "wait": 5}
  ]
}
```

时刻不可行返回 **409**，并指出首个失效闸门、到达时刻与等待截止时刻
（`arrival + max_wait`）及原因（`NO_OPEN_WINDOW` / `WAIT_EXCEEDED`）。
`GET /plans/{id}` 可读回方案。

### 4. 在新日历上重放方案 `POST /plans/{plan_id}/replay/{new_calendar_id}`

只读重放，**绝不改写原方案**。成功重放：

```json
{"status": "STILL_VALID", "failed_gate_id": null, "failed_index": null,
 "arrival": null, "wait_deadline": null, "reason": null}
```

失效时返回 `INVALID` 及首个失效闸门（按航程顺序）、到达与等待截止时刻；
新日历缺少闸门时 `reason` 为 `GATE_NOT_IN_CALENDAR`，`arrival` 为 `null`。

## 数据与边界

- 日历、方案均不可变，系统不提供修改/删除接口；重放针对新日历进行。
- 恰在窗口 `end` 到达判为关闭，会尝试后续窗口；无后续窗口即 `NO_OPEN_WINDOW`。
- 窗口可相接（`[10,20)` 与 `[20,30)`），在 `20` 到达落入第二窗。
