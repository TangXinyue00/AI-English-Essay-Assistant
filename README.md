# 📝 AI-English-Essay-Assistant - AI 英语作文批改助手

## 📖 项目简介

**AI-English-Essay-Assistant** 是一个智能英语作文批改与优化工具，基于开源项目 **Astron Agent** 打造。  
它可以自动对英语作文进行 **评分、纠错、润色与改进建议生成**，帮助用户快速提升写作能力。

---

## 🚀 核心能力

- **AI 作文批改**：自动检测语法、拼写、逻辑与语言表达问题  
- **作文评分系统**：从内容、语言、结构三维度进行智能打分  
- **优化与润色**：提供可读性更高、更自然的改写版本  
- **工作流自动化**：基于 Astron Agent 实现从作文输入到结果输出的自动化流程

---

## 🧩 技术特点

- ✅ 基于企业级开源项目 **Astron Agent** 开发  
- ✅ 支持 **本地部署与私有化使用**  
- ✅ 可视化工作流编排与调试  
- ✅ 支持自定义 Prompt、评分规则与输出格式

---

## ⚙️ 快速开始

### ✅ 环境要求

| 资源 | 最低要求 |
|------|-----------|
| CPU | 2 核 |
| 内存 | 4 GiB |
| 磁盘 | 50 GB |

---

### 🧰 快速部署

```bash
# 进入项目根目录
cd astron-agent

# 进入 astronAgent 目录
cd docker/astronAgent

# 复制环境变量配置
cp .env.example .env

# 编辑环境变量
vim .env

# 一键启动部署
docker compose -f docker-compose-with-auth.yaml up -d

```

### 访问应用

待所有容器服务都启动完成后，可访问如下页面：

- Web服务：http://localhost
- 默认账户：admin / 123

## 使用指南

1. 登录系统后，点击「创建」选择「工作流」
2. 导入AI-English-Essay-Assistant工作流，文件位置：docker/astronAgent/AI-English-Essay-Assistant.yml
3. 保存并运行工作流
4. 上传作文获得结果


## 项目说明

本项目基于 [Astron Agent](https://github.com/iflytek/astron-agent) 打造。

如果您有任何建议或发现问题，欢迎提交 Issue 或 Pull Request。

## 开源协议

本项目基于 Apache 2.0 协议开源，可自由商业使用。
