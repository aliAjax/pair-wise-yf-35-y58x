# 反兴奋剂检测与结果管理

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8301`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、执照/同队放行计算、时间窗冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、乐观锁、写事务、派单槽位和人工核对队列。
- `src/service.py`：用例编排、事务化派单/采样、检查官更新级联、旧数据回填。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、派单规则、并发、级联、回填和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8301
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `athlete`：运动员（可带`team`字段）。
- `inspector`：检查官记录，含执照号`license_no`、执照有效期`license_from/license_to`、所属队`team`和记录版本`revision`。
- `assignment`：赛外检查派单，含运动员、检查官、时间窗`window_start/window_end`；派单、样本和案件都通过`inspector_id`共用同一条检查官记录。
- `sample`：检测样本，必须从一条`assigned`状态的派单创建，自动继承检查官快照。
- `case`：结果管理案件，从阳性样本创建时复制检查官快照。

## 派单放行规则

`POST /api/assignments`（角色需为`dispatcher`或`admin`）在同一个`BEGIN IMMEDIATE`写事务内检查：

1. 调度角色权限；
2. 检查当天（时间窗开始日）执照处于有效期内——过期、吊销或执照状态无法确定一律拒绝；
3. 检查官与运动员不得同队（`team`相同即拒绝）；
4. 同一名检查官不得与其它`assigned`派单时间窗重叠；
5. 同一运动员同一检查日期只有一条派单能放行：`dispatch_slots`表以`运动员|检查日期`为唯一键，多名调度员并发提交时只有一条`INSERT`成功，其余返回`409 ConflictError`。

派单取消（`cancel`）或检查官记录更新导致派单取消时，槽位释放，该运动员当天可重新派单。

## 样本与案件联动

- 样本从派单创建（`assignment_id`），派单随即转为`fulfilled`，不能再出第二份样本；样本记录检查官ID与revision快照。
- 检查官记录更新（`inspector`的`update_record`，仅`admin`）会让`revision`加一，并在同一事务内级联：
  - 尚未采样的`assigned`派单自动取消并释放槽位；
  - 已派但**还没出结果**（未到`analyzed/adverse/cleared`）的样本作废为`voided`，标记`needs_redispatch`并释放槽位；
  - 已出结果的样本保持有效；引用该检查官、已`closed`（或申诉中）的案件转为`reconfirm`，必须由`panel`重新确认（`reconfirm`）后才能回到`closed`；未结案件挂`inspector_revision_pending`标记。

## 旧数据回填

历史数据没有执照状态字段。`POST /api/admin/backfill-licenses`（仅`admin`）：

- 按每条派单/样本自己的检查日期重算执照状态：`verified`（当日有效）/`expired`（已过期）/`unknown`（查不到有效期）；
- 样本只挂了旧执照号的，按`license_no`匹配现有检查官记录并补上`inspector_id`；
- 匹配不到检查官、没有检查日期、或执照日期缺失无法判定的，进入人工核对队列`review_queue`，不做猜测；
- 回填幂等：已处理记录（含已入队项）重复执行不再变化。

人工核对：`GET /api/review?status=open`查看队列，`POST /api/review/<id>/resolve`（`admin`或`panel`，可带`note`）关闭。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录（派单、系统作废/转复核、回填均留痕）。

请求身份通过`X-User-Id`和`X-Role`请求头传入。可用角色：`viewer/admin/dispatcher/inspector/lab/panel`。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：完整业务流程、角色权限、执照边界、同队/时间窗冲突、并发派单只放行一条、采样占用派单、取消释放槽位、检查官更新级联（作废/转复核/重新确认）、旧数据回填与人工核对、乐观锁与幂等。

## 局限

身份、实验室结果和听证材料均为原型模型，不替代正式反兴奋剂信息系统或证据鉴定流程。
