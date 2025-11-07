# Astron Agent Helm 部署指南

## 快速部署

### 1. 修改配置

编辑 `astron-agent/values.yaml`，修改关键配置：

```yaml
# 全局配置 - 主机访问地址
global:
  # 主机基础地址，用于 MinIO、Casdoor 等服务的外部访问
  # 例如: http://192.168.1.100 或 http://your-domain.com
  hostBaseAddress: "http://192.168.1.100"  # 使用 NodePort 时必须设置

# 修改域名（使用 Ingress 时）
ingress:
  enabled: true
  hosts:
    - host: your-domain.com

# 修改数据库密码（生产环境必须修改）
postgresql:
  auth:
    password: your-strong-password

mysql:
  auth:
    rootPassword: your-strong-password

# Casdoor 认证服务配置
casdoor:
  service:
    type: NodePort  # 使用 NodePort 暴露到主机
    nodePort: 30800  # Casdoor 端口的 NodePort (30000-32767)
  mysql:
    rootPassword: casdoor_root123  # Casdoor MySQL root 密码（独立配置）
    password: casdoor123  # Casdoor 数据库用户密码
  # HOST_BASE_ADDRESS 环境变量直接使用 global.hostBaseAddress

# MinIO 配置
minio:
  auth:
    rootPassword: your-strong-password
  service:
    type: NodePort  # 使用 NodePort 暴露到主机
    nodePort: 30900  # API 端口的 NodePort (30000-32767)
    consoleNodePort: 30901  # Console 端口的 NodePort
  # MINIO_SERVER_URL 自动配置逻辑：
  # 1. 如果设置了 serverUrl，直接使用
  # 2. 如果设置了 global.hostBaseAddress 且 type=NodePort，使用 ${hostBaseAddress}:${nodePort}
  # 3. 否则使用内部 Service 地址 http://minio:9000
  serverUrl: ""  # 留空使用自动配置
```

### 2. 部署

```bash
# 使用 Helm 安装
helm install astron-agent ./astron-agent \
  --namespace astron-agent \
  --create-namespace

# 或使用开发配置
helm install astron-agent ./astron-agent \
  -n astron-agent \
  --create-namespace \
  -f astron-agent/values-dev.yaml
```

### 3. 查看状态

```bash
kubectl get pods -n astron-agent
kubectl get svc -n astron-agent
kubectl get ingress -n astron-agent
```

### 4. 访问应用

- **生产环境**：配置域名解析后访问 `http://your-domain.com`
- **开发环境**：使用端口转发
  ```bash
  kubectl port-forward -n astron-agent svc/astron-agent-console-frontend 8080:1881
  # 访问 http://localhost:8080
  ```

## 架构说明

### Ingress 路由配置

- `/workflow/v1/chat/completions` → core-workflow（SSE 长连接）
- `/console-api/chat-message` → console-hub（SSE 长连接）
- `/console-api` → console-hub（后端 API）
- `/` → console-frontend（前端页面）

### MinIO 访问方式

**NodePort 方式（推荐用于开发/测试）**：
- API 端口：`http://<node-ip>:30900`
- Console 端口：`http://<node-ip>:30901`
- 自动配置：设置 `global.hostBaseAddress` 和 `minio.service.type=NodePort`

**ClusterIP 方式（仅集群内部访问）**：
- API 端口：`http://minio:9000`
- 适用于所有服务都在集群内部的场景

**LoadBalancer 方式（生产环境推荐）**：
- 通过云服务商的负载均衡器暴露
- 获取外部 IP 后配置 `minio.serverUrl`

### Casdoor 访问方式

**NodePort 方式（推荐用于开发/测试）**：
- 服务端口：`http://<node-ip>:30800`
- 自动配置：设置 `global.hostBaseAddress` 和 `casdoor.service.type=NodePort`

**ClusterIP 方式（仅集群内部访问）**：
- 服务端口：`http://casdoor:8000`
- 适用于所有服务都在集群内部的场景

**LoadBalancer 方式（生产环境推荐）**：
- 通过云服务商的负载均衡器暴露
- 获取外部 IP 后手动配置 `casdoor.env.consoleDomain` 和 `casdoor.env.hostBaseAddress`

### 服务组件

**基础设施**：PostgreSQL、MySQL、Redis、MinIO
**认证服务**：Casdoor
**核心服务**：core-tenant、core-database、core-rpa、core-link、core-aitools、core-agent、core-knowledge、core-workflow
**Console 服务**：console-frontend、console-hub

## 主要配置项

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `global.astronAgentVersion` | 镜像版本 | `latest` |
| `global.imageRegistry` | 镜像仓库地址 | `ghcr.io/iflytek/astron-agent` |
| `global.hostBaseAddress` | 主机基础地址（NodePort 时使用） | `""` |
| `ingress.enabled` | 启用 Ingress | `true` |
| `ingress.hosts[0].host` | 访问域名 | `astron-agent.example.com` |
| `casdoor.service.type` | Casdoor Service 类型 | `ClusterIP` |
| `casdoor.service.nodePort` | Casdoor NodePort | `30800` |
| `minio.service.type` | MinIO Service 类型 | `ClusterIP` |
| `minio.service.nodePort` | MinIO API NodePort | `30900` |
| `minio.service.consoleNodePort` | MinIO Console NodePort | `30901` |
| `minio.serverUrl` | MinIO 外部访问地址（留空自动配置） | `""` |
| `postgresql.persistence.size` | PostgreSQL 存储 | `10Gi` |
| `mysql.persistence.size` | MySQL 存储 | `10Gi` |
| `minio.persistence.size` | MinIO 存储 | `20Gi` |

## 常用命令

```bash
# 查看所有资源
kubectl get all -n astron-agent

# 查看 MinIO Service（检查 NodePort）
kubectl get svc -n astron-agent | grep minio

# 查看日志
kubectl logs -f -n astron-agent deployment/astron-agent-core-agent

# 升级部署
helm upgrade astron-agent ./astron-agent -n astron-agent

# 卸载
helm uninstall astron-agent -n astron-agent
```

## 部署示例

### 示例 1: 使用 NodePort 暴露 MinIO 和 Casdoor（开发/测试环境）

```yaml
# values-nodeport.yaml
global:
  hostBaseAddress: "http://192.168.1.100"  # 替换为你的主机 IP

casdoor:
  service:
    type: NodePort
    nodePort: 30800

minio:
  service:
    type: NodePort
    nodePort: 30900
    consoleNodePort: 30901
  serverUrl: ""  # 留空，自动使用 http://192.168.1.100:30900

ingress:
  enabled: false  # 开发环境可以禁用 Ingress
```

部署命令：
```bash
helm install astron-agent ./astron-agent \
  -n astron-agent \
  --create-namespace \
  -f values-nodeport.yaml
```

访问方式：
- Casdoor: `http://192.168.1.100:30800`
- MinIO API: `http://192.168.1.100:30900`
- MinIO Console: `http://192.168.1.100:30901`
- 前端: `kubectl port-forward -n astron-agent svc/astron-agent-console-frontend 8080:1881`

### 示例 2: 使用 Ingress（生产环境）

```yaml
# values-production.yaml
global:
  hostBaseAddress: ""  # Ingress 不需要

minio:
  service:
    type: ClusterIP  # 使用内部访问
  serverUrl: "https://minio.your-domain.com"  # 如果通过单独的 Ingress 暴露

ingress:
  enabled: true
  hosts:
    - host: your-domain.com
  tls:
    - secretName: astron-agent-tls
      hosts:
        - your-domain.com
```

部署命令：
```bash
helm install astron-agent ./astron-agent \
  -n astron-agent \
  --create-namespace \
  -f values-production.yaml
```

### 示例 3: 使用命令行参数快速部署

```bash
# 使用 NodePort 快速部署 MinIO 和 Casdoor
helm install astron-agent ./astron-agent \
  -n astron-agent \
  --create-namespace \
  --set global.hostBaseAddress="http://192.168.1.100" \
  --set casdoor.service.type=NodePort \
  --set casdoor.service.nodePort=30800 \
  --set minio.service.type=NodePort \
  --set minio.service.nodePort=30900 \
  --set ingress.enabled=false
```

## 系统要求

**最小配置**（开发环境）：
- CPU: 4 核
- 内存: 8 GB
- 存储: 50 GB

**推荐配置**（生产环境）：
- CPU: 16 核
- 内存: 32 GB
- 存储: 200 GB SSD
- 节点数: 3+

## 故障排查

### Pod 启动失败
```bash
kubectl describe pod <pod-name> -n astron-agent
kubectl logs <pod-name> -n astron-agent
```

### 服务无法访问
```bash
kubectl get svc -n astron-agent
kubectl get endpoints -n astron-agent
kubectl describe ingress -n astron-agent
```

### 存储问题
```bash
kubectl get pvc -n astron-agent
kubectl describe pvc <pvc-name> -n astron-agent
```

## 安全建议

1. **生产环境必须修改默认密码** ⚠️
2. 启用 TLS：配置 `ingress.tls` 和 `cert-manager`
3. 使用 Kubernetes Secrets 管理敏感信息
4. 定期备份数据库和对象存储
5. 配置 NetworkPolicy 限制 Pod 间通信

## 备份和恢复

### 备份数据库
```bash
# PostgreSQL
kubectl exec -n astron-agent astron-agent-postgres-0 -- \
  pg_dump -U spark sparkdb_manager > backup-$(date +%Y%m%d).sql

# MySQL
kubectl exec -n astron-agent astron-agent-mysql-0 -- \
  mysqldump -u root -p<password> --all-databases > backup-$(date +%Y%m%d).sql
```

### 恢复数据库
```bash
# PostgreSQL
kubectl exec -i -n astron-agent astron-agent-postgres-0 -- \
  psql -U spark sparkdb_manager < backup-20240101.sql

# MySQL
kubectl exec -i -n astron-agent astron-agent-mysql-0 -- \
  mysql -u root -p<password> < backup-20240101.sql
```

## 相关资源

- [Kubernetes 官方文档](https://kubernetes.io/docs/)
- [Helm 官方文档](https://helm.sh/docs/)
- [Astron Agent GitHub](https://github.com/iflytek/astron-agent)
