# cc-manifest-lab

CC_Codex 二期（26.11）**数据工程**产品线：**湖区落地清单台**（Litestar + Preact CDN + SQLite）。

## 一句话骨架（自有，非外仓抄题）

**湖区落地批次 → 对象指纹登记 → 清单闭合 → 可见性对账**

与现有七线正交（不是采集目录、ETL 管道、同步源、解析入库、编排流、种子摄取、留存裁撤）。
不碰外仓常见指纹（Feed 归并 / CDC 游标 / 质检签发 / Slicegate 类仓名）。

## 领域

- **所属领域**：数据工程
- **子方向**：数仓与数据湖（落地清单 / 对象登记闭环）

## 技术栈

| 层 | 选型 |
|----|------|
| 后端 | Python 3.12、Litestar、Uvicorn、SQLite |
| 前端 | 原生 JS SPA（全中文，侧栏），靛青竖纹 + 亮青强调；不依赖外网 CDN |
| 测试 | pytest + httpx（本地秒级闭环，优先于 compose） |

## 基线已通主链

1. 湖区落地批次列表（收集中 / 已闭合）
2. 向批次登记对象键与内容指纹
3. 闭合清单（对象数固化，拒绝空批）
4. 可见性对账报告（闭合批 vs 开放批 vs 孤儿对象）

预留切口（未做，供后续功能题）：清单换版生效窗、闭合作废回开放、批量失败闭互斥、跨区依赖冻结、清单回灌等。

## 可见性对账（重算口径）

- **单一快照**：错位列表、报告详情、批次水线、对账复跑台四处读同一份快照，数字必须相同。
- **落库形态：整笔回滚替换**。同一截至日先清旧摘要与旧错位明细，再写本次快照；
  失败整笔回滚、旧摘要原样保留。旧错位不跨报告悬挂，只在本次报告内以「已消除」留痕一行。
- **写锁**：重算走 `BEGIN IMMEDIATE`（`app/db.py:get_db_immediate`），库开 WAL，
  两名值班并发时串行替换，不会双写摘要。
- **对齐校验**：写库前回读实物表逐项复核摘要（已闭合批/收集中批/对象/孤儿/错位/明细偏差），偏一项即停，接口吐短句。
- **复跑**：`--rounds 2` 连续两轮逐项一致才落库通过。

```bash
# 错位列表 / 报告详情 / 批次水线 / 对账复跑台
curl -s -b cookie.txt localhost:6900/api/visibility-reports
curl -s -b cookie.txt localhost:6900/api/visibility-reports/1
curl -s -b cookie.txt 'localhost:6900/api/visibility-waterline?as_of_date=2026-11-16'
curl -s -b cookie.txt -X POST localhost:6900/api/visibility-reconcile \
  -H 'Content-Type: application/json' -d '{"as_of_date":"2026-11-16","rounds":2}'

# 编排侧复跑校验（数字不稳非零退出）
DATABASE_PATH=/app/instance/manifest_lab.db python scripts/reconcile_check.py --rounds 2
```

复盘步骤与验收口径见 [docs/postmortem-visibility.md](docs/postmortem-visibility.md)。

## 本地（推荐反馈环）

```powershell
cd claude-workspace\cc-manifest-lab
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
python run.py
```

http://127.0.0.1:6900/login · **admin / 123456**

## Docker

```powershell
docker compose up -d --build
```

端口 **6900**。冒烟：`python ..\scripts\_smoke_lab_access.py --dir .`
