# Gate report — PASS

Total: 221.3s · PASS 67 / FAIL 0 / SKIP 9

| Layer | PASS | FAIL | SKIP | Duration |
|---|---|---|---|---|
| G0 | 3 | 0 | 0 | 11.148s |
| G1 | 20 | 0 | 0 | 101.548s |
| G2 | 0 | 0 | 3 | 20.684s |
| G3 | 3 | 0 | 0 | 47.458s |
| G4 | 2 | 0 | 1 | 15.339s |
| G5 | 39 | 0 | 5 | 6.348s |

## Skips (registered)

- tests.gate.test_g2_migrations.TestG2::test_upgrade_head: SKIP(registered) G2.migrations: 已知缺陷 — 真实缺陷：迁移 0009 对分区表 events 执行 CREATE INDEX CONCURRENTLY，PG 拒绝（cannot create index on partitioned table ... concurrently），全新库 alembic upgrade head 必败。修复方向：去掉 CONCUR
- tests.gate.test_g2_migrations.TestG2::test_downgrade_base: SKIP(registered) G2.migrations: 已知缺陷 — 真实缺陷：迁移 0009 对分区表 events 执行 CREATE INDEX CONCURRENTLY，PG 拒绝（cannot create index on partitioned table ... concurrently），全新库 alembic upgrade head 必败。修复方向：去掉 CONCUR
- tests.gate.test_g2_migrations.TestG2::test_migrations_match_orm_schema: SKIP(registered) G2.migrations: 已知缺陷 — 真实缺陷：迁移 0009 对分区表 events 执行 CREATE INDEX CONCURRENTLY，PG 拒绝（cannot create index on partitioned table ... concurrently），全新库 alembic upgrade head 必败。修复方向：去掉 CONCUR
- tests.gate.test_g4_functional.TestG4::test_g4b_issue_pr_chain_deferred: SKIP(registered) G4b.issue_pr_chain: P2 未实施（fake git remote + stub 回包 fixtures，DESIGN §5.5.1） — P2 待实施：issue→PR 业务链路需 fake git remote + 确定性 stub 回包 fixtures（DESIGN §5.5.1）
- tests.test_layer_isolation.TestLayerIsolationAudit::test_audit_passes: upstream-sync audit requires ORCHESTRATORD_CI=1 (runs PyGit2 layer check, slow ~10s)
- tests.test_layer_isolation.TestPatchSeriesIntegrity::test_series_has_entries: upstream patch series 58ea488 is not present in this checkout
- tests.test_layer_isolation.TestPatchSeriesIntegrity::test_patch_file_exists: upstream patch series 58ea488 is not present in this checkout
- tests.test_layer_isolation.TestPatchSeriesIntegrity::test_metadata_status_valid: metadata directory not present in this checkout
- tests.test_layer_isolation.TestPatchSeriesIntegrity::test_patch_has_content: upstream patch series 58ea488 is not present in this checkout