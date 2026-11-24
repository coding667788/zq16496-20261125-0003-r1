# vaultdrill

`vaultdrill` 是一个完全离线的灾后恢复方案复核工具，用来回答两个问题：

1. 给定不可变增量快照目录和恢复 `PLAN`，每个对象实际会从哪条快照链恢复、哪些组最终可读、哪些对象/工作集不可恢复，以及最小失败证据是什么。
2. 为样例中的 **14 个受影响工作集**，从候选动作里选择 **至多 18 个动作**；保证强制对象全部恢复、不带出仍封存对象，并依次最小化：
   **停工分钟总和 → 动作数 → 升序 ID 序列**。

仅依赖 Windows 上的 Python 3.12 标准库；所有 JSON 均按 UTF-8 读写，不访问网络。

## 快速开始

在仓库根目录执行：

```bat
python vaultdrill.py evaluate examples\evaluate examples\evaluate\plan.json
python vaultdrill.py recommend examples\recommend\case.json
```

Git Bash / macOS / Linux：

```bash
python vaultdrill.py evaluate examples/evaluate/case.json examples/evaluate/plan.json
python vaultdrill.py recommend examples/recommend/case.json
```

也可以使用模块入口：

```bash
python -m vaultdrill evaluate CASE PLAN
python -m vaultdrill recommend CASE
```

Windows 还提供 `vaultdrill.cmd`。处理不可信输入目录时，推荐使用隔离模式启动根脚本；启动器只插入受信任的脚本目录，不把当前工作目录加入 `sys.path`：

```bash
python -I vaultdrill.py evaluate CASE PLAN
```

运行测试和穷举对拍：

```bash
python -m unittest discover -s tests -v
python tools/exhaustive_check.py
```

`tools/exhaustive_check.py` 会生成 300 个小规模随机集合覆盖/节点区域用例，把动态规划结果与全部候选子集枚举逐一比较；随后还会对内置的 18 候选样例执行 `2^18` 子集穷举对拍。

## CLI

### `evaluate CASE PLAN`

`CASE` 可以是 case JSON 文件，也可以是包含 `case.json` 的目录。`PLAN` 可以是 plan JSON 文件或包含 `plan.json` 的目录。

```bash
python vaultdrill.py evaluate examples/evaluate/case.json examples/evaluate/plan.json
```

输出 `vaultdrill.evaluation.v1`，主要包含：

- `summary`：工作集数、可恢复/批准数量、失败数量、总批准结论。
- `recoverable_entities`：可恢复工作集和对象的扁平清单。
- `actions[].brought_objects`：执行该快照会实体化带出的全部有效对象。
- `actions[].sealed_objects` / `SEALED_OBJECT_BROUGHT_OUT`：会被带出的封存对象证据。
- `work_sets[].assignments[].objects[]`：每个对象的：
  - 稳定 `object_id`、最终 `name`、`digest`；
  - `source_snapshot`：真正提供对象摘要的快照；
  - `snapshot_chain`：从选中增量到完整快照的父链；
  - `table_trace`：链上每层对象表/删除标记为何胜出或被忽略；
  - `allowed_groups`、`denied_groups`；
  - `permission_windows` 和封存状态。
- `failures`：扁平化的最小失败证据。

退出码：

- `0`：JSON 报告已生成；方案本身是否批准看 `summary.approved`。
- `2`：输入 JSON/schema/路径错误，stdout 是 `{"ok": false, "error": ...}`。
- `3`：仅在传入 `--strict` 且方案未批准时返回。

### `recommend CASE`

```bash
python vaultdrill.py recommend examples/recommend/case.json
python vaultdrill.py recommend examples/recommend/case.json --max-actions 18
```

结果包含：

- `feasible`
- `selected_action_ids`
- `objective.downtime_minutes` / `action_count`
- `work_set_assignments`
- 可直接作为 `PLAN` 使用的 `plan`
- 对生成计划再次执行 `evaluate` 的完整 `evaluation`
- 每个候选动作可覆盖哪些工作集、被拒绝的原因

两个子命令都支持 `--out PATH`，在 stdout 输出之外把完整 UTF-8 JSON 结果写入文件；`recommend` 也允许把输出路径作为第二个位置参数。

退出码：

- `0`：找到可行建议。
- `4`：在动作上限和节点区域约束下无可行建议；结果含 `minimal_failure_evidence`。
- `2`：输入错误。
- `3`：传入 `--strict` 且无可行建议。

## 输入模型

### Case

```jsonc
{
  "schema": "vaultdrill.case.v1",
  "id": "example",
  "snapshot_dir": "snapshots",       // 相对于 case.json
  "rule_version_strategy": "source", // source | selected | intersection
  "default_read_groups": ["grp-drill-readers"],
  "window": { "id": "night", "start": "...", "end": "..." },
  "nodes": [
    { "id": "node-1", "regions": ["cn-north", "cn-south"] }
  ],
  "access_rules": { "versions": { "rv-1": { /* ... */ } } },
  "work_sets": [
    {
      "id": "WS01",
      "objects": ["obj-01", "obj-02"],
      "mandatory_objects": ["obj-01", "obj-02"],
      "read_groups": ["grp-drill-readers"]
    }
  ],
  "candidate_actions": [
    {
      "id": "A01",
      "snapshot": "inc-safe",
      "node": "node-1",
      "region": "cn-north",
      "window_id": "night",
      "at": "2026-12-08T02:30:00Z",
      "downtime_minutes": 12,
      "work_sets": ["WS01"]
    }
  ]
}
```

`objects` 是恢复该工作集时一起恢复、必须求许可窗口交集的对象集合。`mandatory_objects` 省略时默认等于 `objects`。

### 快照目录

`snapshot_dir` 中的每个 `*.json` 是一份不可变快照；也兼容 `<snapshot-id>/snapshot.json` 或 `manifest.json`。

```jsonc
{
  "id": "inc-004",
  "parent": "inc-003",
  "kind": "incremental",            // full | incremental；可按 parent 推断
  "access_rule_version": "rv-2026-11",
  "recovery_window": {
    "start": "2026-12-08T00:00:00Z",
    "end": "2026-12-09T00:00:00Z"
  },
  "accessible_regions": ["cn-north", "cn-south"],
  "objects": {
    "obj-readd": {
      "name": "new-name.docx",      // 名称可改
      "digest": "sha256:..."        // 稳定 ID 不变
    }
  },
  "delete_markers": [
    { "id": "obj-masked" }
  ]
}
```

也支持 `object_digests`、`object_names`、`deletions`、`deleted_objects`、`regions`、`parent_id` 等常见别名字段。

### 访问规则

```jsonc
{
  "versions": {
    "rv-1": {
      "default_object_policy": {
        "allowed_groups": ["grp-drill-readers"],
        "sealed": false,
        "permission_window": { "start": "...", "end": "..." }
      },
      "objects": {
        "obj-sealed-1": { "allowed_groups": [], "sealed": true },
        "obj-a": {
          "allowed_groups": ["grp-audit"],
          "sealed_until": "2026-12-10T00:00:00Z",
          "permission_window": { "start": "...", "end": "..." }
        }
      },
      "groups": {
        "grp-audit": { "may_read": ["obj-a"] }
      }
    }
  }
}
```

找不到规则版本时采用安全默认：无读组、视为封存并报告 `UNKNOWN_RULE_VERSION`。规则版本已知但缺少某对象策略时，默认无读组且不推定封存；可通过该版本的 `default_object_policy` 或 case 级 `default_object_policy` 改变默认值。

### Plan

```jsonc
{
  "schema": "vaultdrill.plan.v1",
  "id": "plan-1",
  "at": "2026-12-08T02:30:00Z",
  "window": { "id": "night" },
  "read_groups": ["grp-drill-readers"],
  "actions": [
    {
      "id": "P1",
      "candidate_id": "A01", // 也可直接写 snapshot/node/region 等字段
      "work_sets": ["WS01"],
      "read_groups": ["grp-drill-readers"]
    }
  ]
}
```

## 核心语义

### 稳定对象 ID 与快照链

解析对象时从选中快照沿 `parent` 向完整快照走：

1. 最近的活动对象表条目胜出，因此对象改名或新摘要不会改变身份。
2. 最近的删除标记胜出，并遮住该继承路径上更早完整/增量快照中的副本。
3. 删除标记不是全局删除：兄弟分支不经过该标记，仍可能带出旧对象。
4. 即使对象已在靠近选中端的层命中，也会继续验证父链是否最终到达 `kind: "full"`。
5. 链断裂、父快照缺失、父引用成环、终点不是 full，都会给出结构性证据。
6. 为确保实际可重建，链上快照都会检查区域和恢复时间窗；报告也单独给出 `full_snapshot`。

样例 `examples/evaluate` 明确覆盖“父层对象表行被删除，随后同一稳定 ID 又新增”：

```text
inc-005-latest
  -> inc-004-readd-same-stable-id   # obj-readd 以 sha256:readd-v2 重新出现并胜出
  -> inc-003-parent-table-delete    # 同一 ID 的删除标记
  -> inc-002-rename
  -> full-001                       # 旧副本仍在，但被删除/新增历史覆盖
```

### 许可窗口交集

所有时间均解析为 UTC，窗口为半开区间 `[start, end)`。同一工作集的所有对象许可窗口必须有非空交集，且恢复时刻必须落在交集中。

当交集为空时，报告 `EMPTY_PERMISSION_INTERSECTION`，并只给出最小证据：

- 如果某个窗口自身 `start >= end`，给出这一个窗口；
- 否则给出使最大起点和最小终点相交冲突的两个对象窗口。

样例中的 `ws-no-common-window` 即：`obj-window-a` 和 `obj-window-b` 各自都能恢复，但窗口不相交。

### 封存对象与错误用户组

动作会实体化选中快照链上的全部有效对象，而不只检查工作集列出的对象。只要任一有效对象在恢复时刻仍满足 `sealed: true`、`legal_hold: true` 或未过 `sealed_until`，该动作就不可用于 `recommend`，`evaluate` 会报告 `SEALED_OBJECT_BROUGHT_OUT`。

最终读组默认取**提供对象摘要的源快照**的 `access_rule_version`。可通过 `rule_version_strategy` 改为：

- `selected`：使用选中快照的规则版本；
- `intersection`：源版本和选中版本的允许组取交集，任一路径判定封存即封存。

若动作/工作集请求的组不在最终 `allowed_groups` 中，报告 `GROUP_NOT_AUTHORIZED` 与 `denied_groups`。

### 节点、区域和窗口

同一 `node` 在同一个恢复窗口内不能使用两个区域。满足以下任一条件即视为同一窗口约束组：

- `window_id` 相同；
- 动作显式时间区间重叠；
- 都未提供窗口身份而落入 case/plan 的默认窗口。

推荐算法用并查集把同节点的相同窗口或重叠区间合并成约束键，再在动态规划状态中记录该键已选定的区域。多个工作集/动作可以共享节点；区域相同即允许。

### 推荐算法与最优性

1. 对每个候选动作、每个可服务工作集实际运行评估器，得到可行覆盖位掩码。
2. 排除会带出封存对象、区域/时间/组不合法的动作。
3. 用 14 位工作集覆盖掩码和“节点窗口 → 区域”绑定作为 DP 状态。
4. 按自然升序 ID 依次决定跳过/选择候选；目标键为  
   `(停工分钟总和, 动作数, 升序 ID 序列)`。
5. 在覆盖全部 14 个工作集且动作数不超过 18 的状态中取最小目标键。

ID 的升序比较使用自然排序（`A2 < A10`）；样例使用 `A01` 等零填充 ID，因此自然序与字典序一致。

小规模场景可用 `--exhaustive` 枚举全部候选子集；测试中的独立穷举实现不复用 DP 状态，只复用候选可行性评估。

## 内置样例

### 边界复核样例

`examples/evaluate/case.json` + `examples/evaluate/plan.json` 覆盖：

- 对象改名但 ID 稳定；
- 父层对象表删除后同 ID 再新增；
- 删除标记遮住继承路径上的旧副本；
- 两个对象分别可恢复但无共同许可窗口；
- 请求错误用户组；
- 快照仅允许特定区域；
- 同节点同窗口使用两个区域；
- 兄弟增量未继承删除标记，从而带出仍封存对象。

该计划整体 `approved: false`，但 `ws-good-rename-readd` 的两个对象链和读组均正确。

### 14 个工作集推荐样例

`examples/recommend/case.json` 恰好定义 14 个工作集和 18 个候选动作。运行结果为：

```text
selected: A08, A11, A14
downtime: 76 minutes
actions:  3
evaluation: approved=true, violations=0
```

其中：

- `A17` 虽然只要 5 分钟，但使用 `rec-full`，会带出 `obj-sealed-1/2`，被封存保护拒绝；
- `A18` 使用仅 `cn-south` 可访问的快照却选择 `cn-north`，被区域检查拒绝；
- `A02` 等低价 `cn-south` 动作与同节点 `node-1` 的 `cn-north` 动作在同一窗口冲突；
- `A08` 与 `A11` 共享 `node-1` 且同为 `cn-north`，因此允许共享节点。

DP 结果与 `--exhaustive` / `tools/exhaustive_check.py` 的全子集枚举一致。

## 目录结构

```text
vaultdrill/
  __main__.py      # python -m vaultdrill
  cli.py           # argparse JSON CLI
  loader.py        # UTF-8 JSON case/plan/snapshot loading
  chain.py         # parent chain, stable-ID resolution, materialized view
  policy.py        # access-rule versions, groups, seal and license windows
  evaluator.py     # evaluate CASE PLAN
  recommend.py     # exact DP + small exhaustive oracle
  timeutil.py      # UTC ISO-8601 windows and minimal intersection witness
examples/
  evaluate/        # chain/security/window edge cases
  recommend/       # 14 work sets and 18 candidate actions
tests/
  test_vaultdrill.py
tools/
  exhaustive_check.py
```
