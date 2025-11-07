# 部署常见问题 FAQ

本文档收集了 Astron Agent 部署过程中的常见问题和解决方案。

---

## 1. 怎么升级项目？

如果您已经部署了 Astron Agent，想要升级到最新版本，请按照以下步骤操作：

### 升级步骤

```bash
# 进入 astronAgent 目录
cd docker/astronAgent

# 停止所有服务（包含 Casdoor）
docker compose -f docker-compose-with-auth.yaml down

# 拉取新代码
git fetch
git pull

# 拉取新镜像
docker compose -f docker-compose-with-auth.yaml pull

# 重新按照部署文档配置启动
# 请参考 DEPLOYMENT_GUIDE_WITH_AUTH_zh.md 进行配置和启动
```

### 注意事项

- 升级前建议备份重要数据
- 如果您使用的是不带认证的版本，请将 `docker-compose-with-auth.yaml` 替换为 `docker-compose.yaml`
- 升级后请检查配置文件是否需要更新
- 确保所有环境变量配置正确后再启动服务

---

## 相关文档

- [部署指南（带认证）](./DEPLOYMENT_GUIDE_WITH_AUTH_zh.md)
- [部署指南（不带认证）](./DEPLOYMENT_GUIDE_zh.md)

