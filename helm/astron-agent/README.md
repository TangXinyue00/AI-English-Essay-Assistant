# Astron Agent Helm Chart

这是 Astron Agent 的官方 Kubernetes Helm Chart，用于在 Kubernetes 集群中部署完整的 AI Agent Builder 平台。

## 概述

Astron Agent 是一个功能强大的 AI Agent 构建平台，包含以下组件：

### 基础设施服务
- **PostgreSQL**: 主数据库，用于存储结构化数据
- **MySQL**: 辅助数据库，用于各个微服务
- **Redis**: 缓存和会话存储
- **MinIO**: 对象存储服务

### 认证服务
- **Casdoor**: 统一身份认证和授权服务
- **Casdoor MySQL**: Casdoor 专用数据库

### 核心业务服务
- **core-tenant**: 租户管理服务
- **core-database**: 内存数据库服务
- **core-rpa**: RPA 插件服务
- **core-link**: 链接插件服务
- **core-aitools**: AI 工具插件服务
- **core-agent**: Agent 核心服务
- **core-knowledge**: 知识库服务
- **core-workflow**: 工作流服务

### Console 服务
- **console-frontend**: 前端界面
- **console-hub**: 后端服务
- **nginx**: 反向代理和负载均衡

## 前置条件

- Kubernetes 1.19+
- Helm 3.2.0+
- 持久化存储支持（StorageClass）
- 至少 8GB 可用内存
- 至少 4 核 CPU

## 安装

### 重要：安装前准备 MySQL 初始化脚本

在安装 Chart 之前，需要在 **Kubernetes 节点**上准备 MySQL 初始化脚本。

在 MySQL Pod 可能被调度到的每个节点上执行以下操作：

```bash
# 创建目录
sudo mkdir -p /data/mysql-init

# 复制 SQL 文件（按字母顺序执行）
sudo cp helm/astron-agent/files/mysql/schema.sql /data/mysql-init/01-schema.sql
sudo cp helm/astron-agent/files/mysql/agent.sql /data/mysql-init/02-agent.sql
sudo cp helm/astron-agent/files/mysql/link.sql /data/mysql-init/03-link.sql
sudo cp helm/astron-agent/files/mysql/tenant.sql /data/mysql-init/04-tenant.sql
sudo cp helm/astron-agent/files/mysql/workflow.sql /data/mysql-init/05-workflow.sql

# 设置权限
sudo chmod 644 /data/mysql-init/*.sql
```

**多节点集群说明**：
- 方式 1：在所有节点上准备文件
- 方式 2：使用 `nodeSelector` 将 MySQL 固定到特定节点（推荐）

如果使用方式 2，在 `values.yaml` 中配置：
```yaml
mysql:
  nodeSelector:
    kubernetes.io/hostname: your-node-name
```

### 1. 添加 Helm 仓库（如果可用）

```bash
helm repo add astron-agent https://your-helm-repo-url
helm repo update
```

### 2. 本地安装

```bash
# 克隆仓库
git clone https://github.com/iflytek/astron-agent.git
cd astron-agent/helm/astron-agent

# 安装 Chart
helm install astron-agent . -n astron-agent --create-namespace
```

### 3. 使用自定义配置安装

创建 `custom-values.yaml` 文件：

```yaml
# 自定义配置示例
global:
  astronAgentVersion: "v1.0.0"
  storageClass: "standard"

nginx:
  service:
    type: LoadBalancer

ingress:
  enabled: true
  className: nginx
  hosts:
    - host: astron-agent.example.com
      paths:
        - path: /
          pathType: Prefix
  tls:
    - secretName: astron-agent-tls
      hosts:
        - astron-agent.example.com

minio:
  auth:
    rootUser: admin
    rootPassword: YourSecurePassword123

mysql:
  auth:
    rootPassword: YourMySQLPassword123

postgresql:
  auth:
    password: YourPostgreSQLPassword123
```

使用自定义配置安装：

```bash
helm install astron-agent . -n astron-agent --create-namespace -f custom-values.yaml
```

## 配置说明

### 全局配置

| 参数 | 描述 | 默认值 |
|------|------|--------|
| `global.imageRegistry` | 镜像仓库地址 | `ghcr.io/iflytek/astron-agent` |
| `global.imagePullPolicy` | 镜像拉取策略 | `IfNotPresent` |
| `global.storageClass` | 存储类名称 | `""` |
| `global.astronAgentVersion` | Astron Agent 版本 | `latest` |

### 基础设施服务

#### PostgreSQL

| 参数 | 描述 | 默认值 |
|------|------|--------|
| `postgresql.enabled` | 是否启用 PostgreSQL | `true` |
| `postgresql.replicaCount` | 副本数量 | `1` |
| `postgresql.persistence.enabled` | 是否启用持久化 | `true` |
| `postgresql.persistence.size` | 存储大小 | `10Gi` |
| `postgresql.auth.database` | 数据库名称 | `sparkdb_manager` |
| `postgresql.auth.username` | 用户名 | `spark` |
| `postgresql.auth.password` | 密码 | `spark123` |

#### MySQL

| 参数 | 描述 | 默认值 |
|------|------|--------|
| `mysql.enabled` | 是否启用 MySQL | `true` |
| `mysql.replicaCount` | 副本数量 | `1` |
| `mysql.persistence.enabled` | 是否启用持久化 | `true` |
| `mysql.persistence.size` | 存储大小 | `10Gi` |
| `mysql.auth.rootPassword` | Root 密码 | `root123` |

#### Redis

| 参数 | 描述 | 默认值 |
|------|------|--------|
| `redis.enabled` | 是否启用 Redis | `true` |
| `redis.replicaCount` | 副本数量 | `1` |
| `redis.persistence.enabled` | 是否启用持久化 | `true` |
| `redis.persistence.size` | 存储大小 | `5Gi` |
| `redis.auth.password` | 密码（可选） | `""` |

#### MinIO

| 参数 | 描述 | 默认值 |
|------|------|--------|
| `minio.enabled` | 是否启用 MinIO | `true` |
| `minio.replicaCount` | 副本数量 | `1` |
| `minio.persistence.enabled` | 是否启用持久化 | `true` |
| `minio.persistence.size` | 存储大小 | `20Gi` |
| `minio.auth.rootUser` | Root 用户名 | `minioadmin` |
| `minio.auth.rootPassword` | Root 密码 | `minioadmin123` |
| `minio.service.type` | Service 类型 | `ClusterIP` |
| `minio.service.apiPort` | API 端口 | `9000` |
| `minio.service.consolePort` | Console 端口 | `9001` |

### 认证服务

#### Casdoor

| 参数 | 描述 | 默认值 |
|------|------|--------|
| `casdoor.enabled` | 是否启用 Casdoor | `true` |
| `casdoor.replicaCount` | 副本数量 | `1` |
| `casdoor.service.port` | 服务端口 | `8000` |
| `casdoor.env.consoleDomain` | Console 域名 | `http://localhost` |
| `casdoor.mysql.database` | 数据库名称 | `casdoor` |
| `casdoor.mysql.username` | 数据库用户名 | `casdoor` |
| `casdoor.mysql.password` | 数据库密码 | `casdoor123` |

### Ingress 配置

| 参数 | 描述 | 默认值 |
|------|------|--------|
| `ingress.enabled` | 是否启用 Ingress | `false` |
| `ingress.className` | Ingress 类名 | `nginx` |
| `ingress.hosts` | 主机配置 | `[]` |
| `ingress.tls` | TLS 配置 | `[]` |

## 访问应用

### 通过 LoadBalancer（如果配置）

```bash
export SERVICE_IP=$(kubectl get svc --namespace astron-agent astron-agent-nginx --template "{{ range (index .status.loadBalancer.ingress 0) }}{{.}}{{ end }}")
echo "访问 URL: http://$SERVICE_IP"
```

### 通过 Port Forward

```bash
kubectl port-forward --namespace astron-agent svc/astron-agent-nginx 8080:80
```

然后在浏览器中访问: http://localhost:8080

### 通过 Ingress

如果配置了 Ingress，直接访问配置的域名即可。

## 升级

```bash
# 使用新的配置升级
helm upgrade astron-agent . -n astron-agent -f custom-values.yaml

# 升级到新版本
helm upgrade astron-agent . -n astron-agent --set global.astronAgentVersion=v1.1.0
```

## 卸载

```bash
helm uninstall astron-agent -n astron-agent

# 如果需要删除 PVC（会删除所有数据！）
kubectl delete pvc -n astron-agent --all
```

## 故障排查

### 查看 Pod 状态

```bash
kubectl get pods -n astron-agent
```

### 查看 Pod 日志

```bash
# 查看特定 Pod 的日志
kubectl logs -n astron-agent <pod-name>

# 实时查看日志
kubectl logs -n astron-agent <pod-name> -f

# 查看所有 Pod 的日志
kubectl logs -n astron-agent -l app.kubernetes.io/instance=astron-agent --tail=100
```

### 查看 Service

```bash
kubectl get svc -n astron-agent
```

### 查看 PVC 状态

```bash
kubectl get pvc -n astron-agent
```

### 常见问题

#### 1. Pod 一直处于 Pending 状态

检查 PVC 是否成功绑定：
```bash
kubectl describe pvc -n astron-agent
```

确保集群有足够的资源：
```bash
kubectl describe nodes
```

#### 2. 服务启动失败

查看 Pod 日志：
```bash
kubectl logs -n astron-agent <pod-name>
```

检查依赖服务是否正常运行：
```bash
kubectl get pods -n astron-agent | grep -E "postgres|mysql|redis|minio"
```

#### 3. 数据库连接失败

确保数据库服务已启动：
```bash
kubectl get pods -n astron-agent | grep -E "postgres|mysql"
```

检查 Secret 配置是否正确：
```bash
kubectl get secret -n astron-agent
```

## 生产环境建议

1. **修改默认密码**：在 `values.yaml` 中修改所有默认密码
2. **启用持久化存储**：确保所有数据库和 MinIO 启用了持久化
3. **配置资源限制**：根据实际负载调整 CPU 和内存限制
4. **启用 TLS**：配置 Ingress 使用 HTTPS
5. **配置备份**：定期备份数据库和对象存储
6. **监控和日志**：集成 Prometheus 和日志收集系统
7. **高可用**：对关键服务配置多副本

## 开发和贡献

欢迎提交 Issue 和 Pull Request！

- 项目地址: https://github.com/iflytek/astron-agent
- 文档: https://github.com/iflytek/astron-agent/docs

## 许可证

请查看项目根目录的 LICENSE 文件。

## 支持

如有问题，请通过以下方式获取支持：

- GitHub Issues: https://github.com/iflytek/astron-agent/issues
- 社区论坛: [链接]
- 邮件: support@iflytek.com
