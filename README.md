# 文章考核系统 (Article Exam System)

基于 **FastAPI + LLM + RAG + MongoDB** 的智能文章阅读考核系统。输入文章，自动生成三层级考核题目，并通过检索增强生成（RAG）技术实现基于原文证据的智能评分。

## 📋 功能特性

### 📝 智能出题
- 基于布鲁姆教育目标分类法，自动生成 **基础认知(Basic) / 应用分析(Apply) / 高阶思维(Advanced)** 三层级题目
- 支持四种题型：**选择题 / 填空题 / 简答题 / 开放论述题**
- 知识模块化出题，确保全面覆盖文章核心内容
- 支持多模型配置（智谱 GLM 系列、DeepSeek 系列）

### 📚 RAG 增强评分
- **语义切片 (Semantic Chunking)**：智能在句子边界处切分文章，保留语义完整性
- **向量化存储**：调用 Embedding-3 模型将文本转为 1024 维向量，存入 MongoDB
- **检索增强评分**：评分时检索与题目最相关的 Top-2 文章片段，LLM 基于原文证据评分，有效防止 AI 幻觉

### 🎯 考核流程
1. 输入文章链接或粘贴文本 → 2. LLM 清洗与知识提取 → 3. 生成三层级试卷 → 4. 学生在线答题 → 5. RAG 增强智能评分 → 6. 查看成绩与反馈

### 🔧 其他功能
- 支持多种 LLM 模型切换（智谱 Flash、DeepSeek Chat/Reasoner）
- 实时流式出题（SSE）
- 答题进度自动保存
- 客观题自动比对 + 主观题 RAG 评分
- 美观的现代化 Web 界面

## 🏗️ 技术架构

```
┌─────────────────────────────────────────────────────────┐
│                    前端 (静态文件)                         │
│           HTML + CSS + JavaScript (Vanilla)              │
└─────────────────────┬───────────────────────────────────┘
                      │ HTTP / SSE
┌─────────────────────▼───────────────────────────────────┐
│                FastAPI 后端服务 (server.py)               │
│  ┌─────────┐ ┌──────────┐ ┌──────────┐ ┌────────────┐  │
│  │出题引擎  │ │RAG 评分  │ │文本清洗  │ │会话管理    │  │
│  │(LLM)    │ │(向量检索) │ │(LLM)    │ │(Session)  │  │
│  └─────────┘ └──────────┘ └──────────┘ └────────────┘  │
└──────┬──────────────────┬──────────────────┬────────────┘
       │                  │                  │
┌──────▼──────┐  ┌───────▼───────┐  ┌──────▼──────┐
│  LLM API    │  │   MongoDB     │  │ Playwright  │
│ 智谱/DeepSeek│  │ 存储+向量检索  │  │ 文章抓取    │
└─────────────┘  └───────────────┘  └─────────────┘
```

### 技术栈

| 组件 | 技术 |
|------|------|
| 后端框架 | FastAPI (Python) |
| AI 模型 | 智谱 GLM-4-Flash / DeepSeek Chat & Reasoner |
| 向量模型 | 智谱 Embedding-3 (1024 维) |
| 数据库 | MongoDB 8.2+（支持向量搜索） |
| 文章抓取 | Playwright + BeautifulSoup |
| 前端 | 原生 HTML/CSS/JavaScript |

## 🚀 快速开始

### 前置要求

- Python 3.10+
- MongoDB 8.2+（推荐，用于向量搜索功能）
- Chrome / Chromium（用于文章网页抓取）

### 安装

```bash
# 克隆仓库
git clone https://github.com/TMENG888/Article_exam.git
cd Article_exam

# 安装依赖
pip install -r requirements.txt

# 安装 Playwright 浏览器（用于网页文章抓取）
playwright install chromium
```

### 配置

编辑 `data/config.json` 配置 AI 模型：

```json
{
  "models": [
    {
      "name": "智谱 Flash",
      "api_base_url": "https://open.bigmodel.cn/api/paas/v4",
      "api_key": "your-api-key",
      "model": "GLM-4.7-Flash",
      "embedding_model": "embedding-3",
      "enabled": true
    },
    {
      "name": "DeepSeek Chat",
      "api_base_url": "https://api.deepseek.com",
      "api_key": "your-api-key",
      "model": "deepseek-chat",
      "embedding_model": null,
      "enabled": true
    }
  ],
  "default_model_index": 0
}
```

### 运行

```bash
python server.py
```

访问 http://127.0.0.1:8000

## 💡 使用方式

1. **输入文章**：粘贴文章文本或输入网页 URL
2. **选择模型**：在设置中选择出题和评分配置
3. **生成试卷**：系统自动生成三层级考核题目
4. **学生答题**：在线作答所有题目
5. **提交评分**：系统自动评分并展示成绩分析
6. **查看反馈**：查看每道题的得分和详细评语

## 📐 RAG 评分流程

```
学生提交答案
    ↓
遍历每道主观题
    ↓
题目向量化 (Embedding-3)
    ↓
MongoDB 向量检索 Top-2 相关段落
    ↓
构建 RAG Prompt（含原文证据）
    ↓
LLM 基于原文证据评分
    ↓
汇总成绩
```

详细实现说明见 [RAG_IMPLEMENTATION.md](./RAG_IMPLEMENTATION.md)

## 📁 项目结构

```
article-exam/
├── server.py              # FastAPI 后端主程序
├── database.py            # MongoDB 数据库操作 + 文章抓取
├── models.py              # Pydantic 数据模型
├── question_models.txt    # LLM 出题提示词模板
├── requirements.txt       # Python 依赖
├── RAG_IMPLEMENTATION.md  # RAG 系统实现说明
├── data/
│   └── config.json        # AI 模型配置
├── static/
│   ├── index.html         # 前端页面
│   ├── app.js             # 前端逻辑
│   └── styles.css         # 前端样式
└── .gitignore
```

## 🧪 支持的题型

| 题型 | 类型标识 | 考核层次 |
|------|---------|---------|
| 选择题 | choice | 基础/应用 |
| 填空题 | fill_blank | 基础 |
| 简答题 | short_answer | 应用 |
| 开放论述题 | open_ended | 高阶 |

## ⚙️ 配置参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| chunk_size | 500 | 语义切片字符数 |
| overlap | 50 | 切片重叠字符数 |
| embedding_dimensions | 1024 | 向量维度 (256-2048) |
| batch_size | 64 | 向量化批量大小 |
| top_k | 2 | 检索返回最相关段落数 |

## 📈 性能指标

- **切分速度**: ~1000 字/秒
- **向量化延迟**: ~200ms/批 (64 条)
- **检索延迟**: <50ms/题
- **存储开销**: ~4KB/切片（含向量）

## 🔮 后续优化

- [ ] MongoDB `$vectorSearch` 聚合管道加速检索
- [ ] 多路召回（关键词 + 向量搜索）
- [ ] Cross-Encoder 重排序精排检索结果
- [ ] 常见题目检索缓存
- [ ] 增量向量化更新
- [ ] 试卷导出（PDF/Word）

## 📄 许可证

MIT License
