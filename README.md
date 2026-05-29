# SentinelOne → GLPI 资产同步服务

轻量级单机服务，定时从多个 SentinelOne 控制台拉取 Agent 列表，检测新增/变更资产，同步到 GLPI 并通过飞书通知。

## 架构

```
┌──────────────┐
│  S1 (HK)     │──┐     ┌──────────────┐     ┌──────────────┐
└──────────────┘  │     │  SyncService │     │    GLPI      │
                  ├────▶│              │────▶│   REST API   │
┌──────────────┐  │     │  ┌────────┐  │     └──────────────┘
│  S1 (SZ)     │──┘     │  │ Cache  │  │
└──────────────┘        │  │(SQLite)│  │     ┌──────────────┐
                        │  └────────┘  │────▶│ 飞书 Webhook  │
┌──────────────┐        └──────────────┘     └──────────────┘
│  S1 (...)    │──┐          │
└──────────────┘  │   ┌──────┴──────┐
                  └──▶│  FastAPI     │
                      │  + Scheduler │
                      └─────────────┘
```

## 目录结构

```
s1_glpi/
├── main.py              # 入口
├── config.yaml          # 配置文件（需自行填写）
├── config.example.yaml  # 配置示例
├── requirements.txt     # 依赖
├── data/                # SQLite 数据目录（自动创建）
└── app/
    ├── __init__.py
    ├── config.py        # 配置加载（支持多 S1 实例）
    ├── models.py        # 数据模型（含 console_name 字段）
    ├── cache.py         # SQLite 缓存层（自动迁移）
    ├── s1_client.py     # SentinelOne API 客户端（按控制台实例化）
    ├── glpi_client.py   # GLPI API 客户端
    ├── lark_notify.py   # 飞书通知（含控制台来源标签）
    ├── sync.py          # 核心同步逻辑（多控制台合并）
    ├── scheduler.py     # APScheduler 定时调度
    └── api.py           # FastAPI 接口
```

## 快速开始

### 1. 安装依赖

```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. 配置

```bash
cp config.example.yaml config.yaml
# 编辑 config.yaml，填入：
#   - 各 S1 控制台的 base_url 和 api_token
#   - GLPI 地址 + App Token + User Token
#   - 飞书 Webhook URL（可选）
```

### 3. 启动

```bash
python main.py
```

或使用 uvicorn：

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

### 4. 验证

```bash
# 健康检查
curl http://localhost:8000/health

# 查看状态
curl http://localhost:8000/status

# 手动触发同步
curl -X POST http://localhost:8000/sync
```

## 多控制台配置

在 `sentinelone_instances` 列表中配置多个 S1 控制台，每个实例用 `name` 字段区分：

```yaml
sentinelone_instances:
  - name: "HK"
    base_url: "https://hk.sentinelone.net"
    api_token: "YOUR_HK_S1_API_TOKEN"
    page_size: 500
  - name: "SZ"
    base_url: "https://sz.sentinelone.net"
    api_token: "YOUR_SZ_S1_API_TOKEN"
    page_size: 500
```

- `name` 会作为控制台标识，出现在：日志、飞书通知卡片、缓存数据中
- 每个控制台独立拉取，单个控制台拉取失败不影响其他控制台
- 所有控制台的 Agent 合并后统一做差异检测和 GLPI 同步

> 旧配置格式 `sentinelone:`（单对象）仍然兼容，会自动转为单元素列表。

## API 接口

| 方法   | 路径      | 说明         |
|--------|-----------|--------------|
| GET    | /health   | 健康检查     |
| GET    | /status   | 服务状态     |
| POST   | /sync     | 手动触发同步 |

## 配置说明

| 配置项                              | 默认值  | 说明                       |
|-------------------------------------|---------|----------------------------|
| scheduler.interval_seconds          | 300     | 同步间隔（秒）             |
| scheduler.run_on_start              | true    | 启动时是否立即执行一次     |
| sentinelone_instances[].name        | default | 控制台标识（HK/SZ 等）     |
| sentinelone_instances[].page_size   | 500     | S1 API 分页大小            |
| lark.enabled                        | false   | 是否启用飞书通知           |
| lark.notify_types                   | both    | 通知类型                   |
| cache.db_path                       | ./data/cache.db | SQLite 路径         |

### GLPI 字段映射

`glpi.field_mapping` 定义了 S1 字段到 GLPI Computer 字段的映射关系，可根据需要增减：

```yaml
glpi:
  field_mapping:
    computerName: "name"
    osName: "operatingsystems_id"
    domain: "domains_id"
    modelName: "computermodels_id"
    serialNumber: "serial"
    ip: "ip_address"
    agentVersion: "comment"
```

## 扩展指南

- **新增 S1 控制台**：在 `config.yaml` 的 `sentinelone_instances` 列表中加一项即可
- **新增数据源**：参考 `s1_client.py`，实现新的 `XxxClient` 并在 `sync.py` 中接入
- **新增目标系统**：参考 `glpi_client.py`，实现新的 `YyyClient`
- **新增通知渠道**：参考 `lark_notify.py`，实现新的通知器
- **新增同步字段**：修改 `models.py` 中的 `COMPARE_FIELDS` 和 `to_cache_dict()`
- **自定义同步策略**：修改 `sync.py` 中的 `_detect_changes()` 和 `_sync_to_glpi()`

## License

MIT
