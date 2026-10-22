# Codex 交接文档 / CODEX_HANDOFF

- Status: in_progress
- Session: a1b2c3d4
- Created: 2026-10-20 22:15
- Tokens at handoff: 203400

## 1. 目标与范围

把支付回调服务的连接池耗尽问题修掉，只改 `payment-service` 的连接池配置与
重试逻辑，不动网关层。不在本次范围内：压测平台搭建、监控大盘。

## 2. 已验证的当前状态

- 仓库 `payment-service`，分支 `fix/callback-pool`，HEAD `9f3ab21`
- 已定位根因：高并发回调场景下连接池上限 20，等待队列无限增长
- `pytest tests/payment -q` 全绿（142 passed）

## 3. 架构与数据流

客户端 → API 网关 → payment-service（回调处理器）→ 第三方支付渠道。
回调处理器每个请求占用一个池连接直到渠道响应返回，慢渠道会把池占满。

## 4. 决策与被否决的方案

- 考虑过把回调改全异步 MQ：迁移成本高，本次不做（备选，见风险）
- 考虑过无上限队列：会把内存打爆，否决
- 选择：连接Pool 20 → 50 + 等待超时 2s + 有限队列 200

## 5. 相关文件与符号

- `src/payment/callback.ts:42` `handleCallback()` 池获取点
- `src/payment/pool.ts:18` `ConnectionPool` 配置
- `config/payment.yaml` `pool.size` / `pool.acquireTimeoutMs`

## 6. 验证命令与结果

```
pytest tests/payment -q        # 142 passed
node scripts/repro-callback.js # 复现脚本：修复前 500 并发 12% 超时，修复后 0
```

## 7. 工作树状态

- `git status`：3 个文件已修改未提交（callback.ts / pool.ts / payment.yaml）
- 无未跟踪文件，无 stash

## 8. 风险与未知

- 50 连接的池对下游渠道的承载能力 UNKNOWN，需要一次真实流量灰度
- MQ 异步化仍是长期方向，本次临时方案可能埋坑

## 9. 唯一的下一步

在 `config/payment.yaml` 把 `pool.size` 改为 50、`acquireTimeoutMs` 改为
2000，跑 `pytest tests/payment -q` 后提交（commit message 用
`fix(payment): bound callback connection pool`）。

## 10. 接手清单

- [ ] `git checkout fix/callback-pool && git status` 核对工作树
- [ ] 读 `src/payment/pool.ts` 确认配置项名称未变
- [ ] 灰度方案需要用户授权后才可推进

## 11. 交接历史

- 2026-10-20 22:15 首次交接：根因已定位，配置待改
