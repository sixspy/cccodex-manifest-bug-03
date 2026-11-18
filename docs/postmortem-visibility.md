# 复盘附页 · 可见性错位条冒消与入库对账漂

本页只记复盘步骤与验收口径，正文实现见 `app/services/visibility.py`。

## 1. 病灶

| 症状 | 根因 | 位置（修前） |
|------|------|--------------|
| 错位条今天冒明天消 | 错位计数掺了 `closed % 2` 墙噪 | `app/services/visibility.py:41` |
| 摘要与列表数字对不齐 | 摘要、错位、水线各算一套，无单一快照 | 同一函数内多条独立 SQL |
| 入库对账漂 | 报告只追加不清理，同日多笔摘要并存 | `visibility_reports` 无同日唯一约束 |
| 两名值班重算双写 | 无写锁，读-算-写不原子 | `get_db` 默认延迟事务 |
| 错位无来源、无新旧差 | 只有计数，没有明细表 | 无 `visibility_mismatches` |

## 2. 落库形态（已拍板）

**整笔回滚替换**：

1. 同一 `as_of_date` 在 `BEGIN IMMEDIATE` 事务内读实物批算快照；
2. 写库前回读实物表逐项复核摘要（已闭合批 / 收集中批 / 对象 / 孤儿 / 错位 / 明细偏差）；
3. 任何一项偏了，抛 `VisibilityError` 短句，**整笔回滚，旧摘要原样保留**；
4. 校验通过后先删同日旧摘要与旧错位明细，再写本次摘要与明细，一笔提交。

因此：**旧错位不跨报告悬挂**；只在本次报告内以 `已消除` 行留痕一次（不参与错位条数）。

> 另一形态（部分留存历史错位、只追加新条）**未采用**，两种落库不同挂。
> 若日后改判为该形态，需同时改 `run_visibility` 的清理段与 `state` 迁移规则，
> 并重跑下文第 4 节全部步骤。

## 3. 复盘步骤（本机）

```bash
# 1) 起服务
python run.py                     # http://127.0.0.1:6900/login  admin / 123456

# 2) 页面四处同查
#    可见性对账（错位列表）→ 报告详情 → 批次水线 → 对账复跑台
#    四处“错位”数字必须相同；报告详情里每行带来源批与 新增/沿用/已消除

# 3) 本机库直查（与页面同数）
sqlite3 instance/manifest_lab.db "
  SELECT id, as_of_date, mismatch_count, prev_mismatch_count, mismatch_delta
    FROM visibility_reports ORDER BY id DESC LIMIT 5;
  SELECT report_id, batch_id, state, manifest_object_count, physical_object_count, delta
    FROM visibility_mismatches ORDER BY report_id DESC, id;
  SELECT COUNT(*) AS same_day_reports
    FROM visibility_reports WHERE as_of_date='2026-11-16';"

# 4) 对账复跑至少两轮，数字不变才算过
curl -s -b cookie.txt -X POST http://127.0.0.1:6900/api/visibility-reconcile \
  -H 'Content-Type: application/json' -d '{"as_of_date":"2026-11-16","rounds":2}'
# 期望：stable=true，runs[0] == runs[1]，counters 与落库摘要一致

# 5) 编排侧（compose）同样复跑两轮对照
docker compose up -d --build
# 重复第 2–4 步；页面水线条与本机库、compose 库三处数字须一致
```

## 4. 验收口径（全过才算过）

- [ ] 错位条只随实物批变化，不再随已闭合批数奇偶冒消（`test_mismatch_count_is_stable_not_wall_noise`）。
- [ ] 同日重算只留一笔摘要（`test_recalc_replaces_same_day_report`）。
- [ ] 旧错位不悬挂：修复后仅留一条 `已消除` 留痕（`test_old_mismatch_does_not_hang_after_recalc`）。
- [ ] 错位列表亮来源批与新旧差（`new_mismatch_count` / `carried_mismatch_count` / `cleared_mismatch_count` / `mismatch_delta`）。
- [ ] 报告详情、水线、复跑台、错位列表四处的错位数字一致（`test_waterline_and_report_share_numbers`）。
- [ ] 并发两名值班同时重算，不双写（`test_concurrent_recalc_single_report_no_double_write`）。
- [ ] 落库校验失败时整笔回滚，旧摘要仍在（`test_failed_recalc_keeps_previous_report`）。
- [ ] 复跑两轮数字不变（`test_reconcile_two_rounds_stable_and_stored`、`test_reconcile_endpoint_two_rounds`）。
- [ ] 失败吐短句（`test_recalc_failure_returns_short_sentence`）。
- [ ] 只改错位列表排序或只改报告详情标题 → 视为未修，验收失败。

## 5. 防回退

- 禁止在 `mismatch` 计算里再加入任何与实物批无关的项（时间、奇偶、随机、缓存）。
- 重算只允许 `get_db_immediate`；只读接口用 `get_db`。
- 库保持 WAL（`init_db` 里设置）；否则并发重算下写者会互等超时。
- 新增对账字段时，摘要文案与 `_assert_aligned` 必须同步改，否则重算会自停。
