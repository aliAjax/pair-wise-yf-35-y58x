# 反兴奋剂检测与结果管理

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8301`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8301
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `athlete`：运动员；`inspector`：检查官；`assignment`：赛外检查派单；`sample`：检测样本；`case`：结果管理案件。

## 派单与检查官联动

赛外检查派单（`assignment`）由调度员（`dispatcher` 角色）创建并放行（`POST /api/assignments/<id>/release`）。放行在一个 `BEGIN IMMEDIATE` 事务内依次校验：

1. 检查官与运动员均为现役；
2. 检查官执照在检查当天有效（`license_expiry >= scheduled_at` 日期）；
3. 检查官与运动员不同队；
4. 同一名检查官在同一时段（默认 24 小时窗口）内没有其他已放行派单。

任一不满足则派单状态置为 `rejected` 并记录原因，不放行、不生成样本；全部通过才置为 `released` 并自动生成关联样本（样本携带 `inspector_id` 与执照快照）。并发调度时只有一个派单能通过双重预约检查。

检查官记录更新（`inspector` 的 `update` 动作）会触发级联：

- 该检查官手上尚未出结果（`scheduled`/`collected`/`sealed`/`in_transit`/`received`）的样本一律置为 `void` 并标记 `needs_redispatch`；
- 引用该检查官的案件置为 `needs_reconfirm`，需由管理员或专案委员执行 `reconfirm` 动作重新确认结论。

旧数据补录（`POST /api/backfill`，仅管理员）：按检查日期（`collected_at`/`scheduled_at`/创建日期）回填样本的执照状态；缺少检查官或执照信息无法判定的样本转人工核对（`needs_manual_verification`）。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `POST /api/assignments/<id>/release`：调度员放行派单。
- `POST /api/backfill`：管理员回填旧样本执照状态。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

身份、实验室结果和听证材料均为原型模型，不替代正式反兴奋剂信息系统或证据鉴定流程。
