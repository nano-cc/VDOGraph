# DoVideo AI Service

AI/ML algorithms for video content understanding.

## 快速开始

### 安装依赖

```bash
pip install -r requirements.txt
```

### 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，填入 SILICONFLOW_API_KEY
```

### 启动服务

```bash
python -m app.main
```

服务将在 `http://localhost:8000` 启动。

### 测试

```bash
python test_service.py
```

## API 文档

启动服务后，访问：
- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc

## 目录结构

```
app/
├── main.py              # FastAPI 入口
├── api/                 # API 路由
│   └── v1/
│       ├── endpoints/   # API 端点
│       └── router.py    # 路由汇总
├── core/                # 核心配置
│   ├── config.py        # 配置管理
│   ├── logging.py       # 日志配置
│   └── exceptions.py    # 异常处理
├── models/              # 数据模型（Pydantic）
├── services/            # 业务逻辑层
├── algorithms/          # 算法层
├── clients/             # 外部服务客户端
└── utils/               # 工具类
```

## 功能

- 实体关系抽取
- 实体消歧
- 社区检测
- Agent 分析（待实现）

## 技术栈

- FastAPI
- NetworkX
- DeepSeek API
- BGE-M3 Embedding
