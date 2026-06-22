"""
文章考核系统 - 后端服务
基于 FastAPI + LLM + MongoDB 实现文章阅读考核
"""
import logging
import os
import json
import uuid
import time
import re
import requests as http_requests
from datetime import datetime
from typing import Optional, Generator, Union
import asyncio
from openai import AsyncOpenAI, OpenAI

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool

# 导入自定义模块
from models import GenerateRequest, GradeRequest, SaveProgressRequest, ConfigRequest
from database import (
    get_collection,
    get_chunks_collection,
    fetch_article_from_url,
    clean_scraping_text,
    is_port_open,
    ensure_chrome_running,
    save_article_chunks,
    search_similar_chunks
)

app = FastAPI(title="文章考核系统")
llm_sem = asyncio.Semaphore(5)
logger = logging.getLogger(__name__)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Configuration (仍用本地 JSON，因为这是服务配置而非业务数据) ---
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)

CONFIG_FILE = os.path.join(DATA_DIR, "config.json")


def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
            # 兼容旧格式（数组）和新格式（字典）
            if isinstance(cfg, list):
                # 旧格式：直接是数组
                return {
                    "models": cfg,
                    "default_model_index": 0
                }
            return cfg
    return {"models": [], "default_model_index": 0}


def save_config(cfg: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def get_model_by_index(index: int) -> dict:
    """根据索引获取模型配置"""
    config = load_config()
    models = config.get("models", [])
    if 0 <= index < len(models):
        return models[index]
    # 如果索引无效，返回默认模型
    return models[0] if models else None


def get_llm_client(config: Optional[dict] = None) -> OpenAI:
    cfg = config or load_config()
    # 如果传入的是完整配置（包含 models），则使用默认模型
    if "models" in cfg:
        default_index = cfg.get("default_model_index", 0)
        model_cfg = cfg["models"][default_index]
    else:
        # 如果传入的是单个模型配置
        model_cfg = cfg
    return OpenAI(api_key=model_cfg["api_key"], base_url=model_cfg["api_base_url"])


# --- Pydantic Models (已从 models.py 导入) ---


# --- Helpers ---

def semantic_chunking(text: str, chunk_size: int = 500, overlap: int = 50) -> list:
    """
    语义切片：基于多级分隔符的中文语义切分算法
    
    分隔符优先级：\n\n (段落) > \n (行) > 。!！ (句末) > ，, (句中) >   (空格)
    确保切分符合中文语义单元，避免断裂
    
    Args:
        text: 输入文本
        chunk_size: 每个切片的目标大小（字符数）
        overlap: 重叠部分大小（字符数）
    
    Returns:
        切片列表，每个切片包含文本、位置信息
    """
    # 定义分隔符优先级列表（从高到低）
    separators = ["\n\n", "\n", "。", "！", "！", "，", ",", " ", ""]
    
    def find_best_split_point(chunk_text: str, target_size: int) -> int:
        """
        寻找最佳切分点：优先在高级别分隔符处切分
        
        Args:
            chunk_text: 待切分的文本块
            target_size: 目标切分位置
        
        Returns:
            最佳切分点位置（相对于 chunk_text 开头）
        """
        # 如果文本长度小于 target_size，直接返回末尾
        if len(chunk_text) <= target_size:
            return len(chunk_text)
        
        # 按优先级尝试各个分隔符
        for sep in separators:
            if not sep:  # 空字符串表示强制切分
                return target_size
            
            # 在 [target_size-50, target_size+50] 范围内查找该分隔符
            search_start = max(0, target_size - 50)
            search_end = min(len(chunk_text), target_size + 50)
            search_range = chunk_text[search_start:search_end]
            
            # 从后往前找（优先保证完整性）
            pos_in_search = search_range.rfind(sep)
            if pos_in_search != -1:
                return search_start + pos_in_search + len(sep)
        
        # 如果都没找到，在目标位置强制切分
        return target_size
    
    chunks = []
    step = chunk_size - overlap
    
    for i in range(0, len(text), step):
        start = i
        # 先取一个较大的窗口用于寻找最佳切分点
        window_end = min(i + chunk_size + 100, len(text))
        window_text = text[start:window_end]
        
        # 寻找最佳切分点
        best_split = find_best_split_point(window_text, chunk_size)
        end = start + best_split
        
        # 提取切片文本
        chunk_text = text[start:end].strip()
        
        # 跳过空切片
        if not chunk_text:
            continue
        
        chunks.append({
            'chunk_index': len(chunks),
            'text': chunk_text,
            'start_pos': start,
            'end_pos': end
        })
        
        # 如果已经到达末尾，退出
        if end >= len(text):
            break
    
    return chunks


async def get_embeddings(texts: list, dimensions: int = 1024) -> list:
    """
    调用智谱 Embedding-3 模型批量获取向量（固定使用智谱，不受默认模型影响）
    
    Args:
        texts: 文本列表
        dimensions: 向量维度（256-2048，默认 1024）
    
    Returns:
        向量列表
    """
    cfg = load_config()
    
    # 【关键修复】始终使用第一个支持 embedding_model 的模型（通常是智谱）
    embedding_model_cfg = None
    for model in cfg.get("models", []):
        if model.get("embedding_model"):
            embedding_model_cfg = model
            break
    
    if not embedding_model_cfg:
        raise HTTPException(status_code=500, detail="未配置任何支持向量检索的模型，请确保至少有一个模型配置了 embedding_model")
    
    embedding_model_name = embedding_model_cfg.get("embedding_model", "embedding-3")
    client = AsyncOpenAI(
        api_key=embedding_model_cfg["api_key"], 
        base_url=embedding_model_cfg["api_base_url"]
    )
    
    try:
        response = await client.embeddings.create(
            model=embedding_model_name,
            input=texts,
            dimensions=dimensions
        )
        return [item.embedding for item in response.data]
    finally:
        await client.close()


def call_llm(prompt: str, system_prompt: str = "", config: Optional[dict] = None, model_index: Optional[int] = None) -> str:
    """
    调用 LLM 生成回复
    :param model_index: 指定使用的模型索引，若为空则使用 config 中的默认模型
    """
    cfg = config or load_config()
    
    # 🔑 关键修改：如果传入了 model_index，优先使用该模型
    if model_index is not None and "models" in cfg:
        models = cfg.get("models", [])
        if 0 <= model_index < len(models):
            model_cfg = models[model_index]
        else:
            model_cfg = models[0] if models else None
    elif "models" in cfg:
        default_index = cfg.get("default_model_index", 0)
        model_cfg = cfg["models"][default_index]
    else:
        model_cfg = cfg

    if not model_cfg or not model_cfg.get("api_key"):
        raise HTTPException(status_code=400, detail="请先配置 API Key，点击右上角设置")

    client = OpenAI(api_key=model_cfg["api_key"], base_url=model_cfg["api_base_url"])
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    for attempt in range(3):
        try:
            response = client.chat.completions.create(
                model=model_cfg["model"],
                messages=messages,
                temperature=0.7,
                response_format={"type": "json_object"}
            )
            return response.choices[0].message.content
        except Exception as e:
            if attempt == 2:
                raise HTTPException(status_code=500, detail=f"AI 服务调用失败：{str(e)}")
            time.sleep(1)


def _get_evidence_level(similarity_pct: float) -> str:
    """
    根据相似度百分比获取证据层级标签
    
    Args:
        similarity_pct: 相似度百分比（0-100）
    
    Returns:
        层级标签字符串
    """
    if similarity_pct > 75:
        return "极强相关"
    elif similarity_pct >= 65:
        return "强相关"
    elif similarity_pct >= 50:
        return "弱相关"
    else:
        return "噪声"


# --- Session Operations (已从 database.py 导入) ---

def save_session(data: dict, session_id: str):
    """插入或更新会话到 MongoDB"""
    from database import save_exam_session
    data["_id"] = session_id  # 用 session_id 作为 _id
    save_exam_session(session_id, data)


def load_session(session_id: str) -> Optional[dict]:
    """从 MongoDB 读取会话"""
    from database import load_exam_session
    doc = load_exam_session(session_id)
    return doc


async def clean_content_with_llm_concurrent(raw_text: str) -> str:
    """使用 GLM-4-Flash-250414 模型并发清洗网页杂质"""
    if not raw_text or len(raw_text) < 150:
        return raw_text

    # 1. 加载配置并初始化异步客户端
    cfg = load_config()
    api_key = cfg.get("api_key")
    if not api_key:
        logger.info("⚠️ 未配置 API Key，跳过 AI 清洗步骤")
        return raw_text

    # 使用异步客户端
    client = AsyncOpenAI(
        api_key=api_key,
        base_url=cfg.get("api_base", "https://open.bigmodel.cn/api/paas/v4/")
    )
    model_name = "glm-4-flash-250414"


    # 2. 将文章切分为 5 份
    global_llm_semaphore = 5
    chunk_size = len(raw_text) // global_llm_semaphore
    chunks = []
    for i in range(global_llm_semaphore):
        start = i * chunk_size
        end = (i + 1) * chunk_size if i < 4 else len(raw_text)
        chunks.append(raw_text[start:end])

    # 3. 定义单个清洗任务
    async def process_chunk(index, chunk_text):
        async with llm_sem:
            try:
                role_content = """你是一个网页正文提取引擎。你的任务是清洗 HTML 转化后的乱码或杂质。
                【执行指令】：
                1. 仅输出该片段中属于"文章正文"的文字。
                2. 严禁修改原文的任何词汇、语气和标点。
                3. 严禁加入你自己的解释、总结或"以下是提取内容"等废话。
                4. 过滤掉广告、导航栏、版权声明、页脚、侧边栏文字。
                5. 如果片段中全是杂质，请输出空字符串。
                
                【输出要求】：
                直接输出提取后的正文原文，不要任何包裹，不要 Markdown 格式块。"""
                response = await client.chat.completions.create(
                    model=model_name,
                    messages=[
                        {"role": "system", "content": role_content},
                        {"role": "user", "content": chunk_text}
                    ],
                    temperature=0.1,
                )
                return response.choices[0].message.content.strip()
            except Exception as e:
                logger.info(f"❌ 块 {index} 处理失败：{e}")
                return chunk_text

    try:
        logger.info(f"🚀 正在使用 {model_name} 并发处理 5 个文本块...")
        tasks = [process_chunk(i, text) for i, text in enumerate(chunks)]
        cleaned_chunks = await asyncio.gather(*tasks)

        full_cleaned_content = "\n".join(cleaned_chunks)

        if len(full_cleaned_content) < 50:
            return raw_text

        return full_cleaned_content

    except Exception as e:
        logger.info(f"❌ 并发清洗总流程失败：{e}")
        return raw_text
    finally:
        await client.close()


# --- API Endpoints ---

@app.get("/api/config")
async def get_config():
    cfg = load_config()
    # 返回所有可用模型列表
    models_info = []
    for idx, model in enumerate(cfg.get("models", [])):
        models_info.append({
            "index": idx,
            "name": model.get("name"),
            "model": model.get("model"),
            "embedding_model": model.get("embedding_model"),
            "enabled": model.get("enabled", True)
        })
    
    return {
        "models": models_info,
        "default_model_index": cfg.get("default_model_index", 0),
        "api_key_set": bool(cfg["models"][0].get("api_key")) if cfg.get("models") else False
    }


@app.post("/api/config")
async def update_config(req: ConfigRequest):
    cfg = load_config()
    models = cfg.get("models", [])
    
    # 1. 获取要修改的模型索引（前端传来的 model_index）
    # 如果没传，则默认修改当前选中的默认模型
    target_idx = req.model_index if req.model_index is not None else cfg.get("default_model_index", 0)
    
    if 0 <= target_idx < len(models):
        # 2. 更新指定索引的模型参数
        if req.api_base_url:
            models[target_idx]["api_base_url"] = req.api_base_url
        if req.api_key:
            models[target_idx]["api_key"] = req.api_key
        if req.model:
            models[target_idx]["model"] = req.model
        
        # 同时将此模型设为默认（因为用户保存时通常希望立即使用）
        cfg["default_model_index"] = target_idx
        
        logger.info(f"已更新索引为 {target_idx} 的模型配置：{models[target_idx]['name']}")
    else:
        raise HTTPException(status_code=400, detail="无效的模型索引")

    save_config(cfg)
    return {"message": "配置已保存", "current_model": models[target_idx]["model"]}


@app.get("/api/models")
async def list_models():
    """获取所有可用模型列表"""
    cfg = load_config()
    models = cfg.get("models", [])
    return {
        "models": [
            {
                "index": idx,
                "name": m.get("name"),
                "model": m.get("model"),
                "embedding_model": m.get("embedding_model"),
                "enabled": m.get("enabled", True)
            }
            for idx, m in enumerate(models)
        ],
        "default_index": cfg.get("default_model_index", 0)
    }


@app.post("/api/models/switch")
async def switch_model(model_index: int):
    """切换到指定模型"""
    cfg = load_config()
    models = cfg.get("models", [])
    
    if not (0 <= model_index < len(models)):
        raise HTTPException(status_code=400, detail="无效的模型索引")
    
    cfg["default_model_index"] = model_index
    save_config(cfg)
    
    return {
        "message": f"已切换到 {models[model_index]['name']}",
        "current_model": models[model_index]
    }


def extract_json_from_llm_response(text: str) -> dict:
    """从 LLM 返回的文本中提取并解析 JSON 对象"""
    try:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            text = re.sub(r'```json\s*|\s*```', '', text).strip()
            text = text.replace('“', '"').replace('”', '"')

            return json.loads(text)
    except json.JSONDecodeError:
        raise ValueError("LLM 返回的内容不包含有效的 JSON 结构")


async def extract_knowledge_map(chunks: list, config: dict, model_index: Optional[int] = None) -> dict:
    """
    【Map-Reduce 模式】从长文中提取核心知识模块
    
    Args:
        chunks: 语义切片列表
        config: LLM 配置
    
    Returns:
        JSON 格式的知识模块列表
    """
    logger.info(f"🗺️ 开始 Map-Reduce 知识图谱提取...")
    
    # 1. Map 阶段：并发提取各块的核心考点
    chunk_size = 5  # 每 5 个 chunk 为一组
    grouped_chunks = [chunks[i:i + chunk_size] for i in range(0, len(chunks), chunk_size)]
    logger.info(f"📦 将 {len(chunks)} 个分块分为 {len(grouped_chunks)} 组进行并行处理")
    
    def process_group(group_index: int, group: list) -> str:
        """处理单个文本组"""
        text = "\n".join([c['text'] for c in group])
        prompt = f"请从以下文本中提取核心知识模块，每个模块包含名称和简要描述。直接以 JSON 数组格式返回：\n\n{text}"
        
        try:
            result = call_llm(prompt, "你是一个知识图谱专家，擅长提取和组织知识结构。", config, model_index=model_index)
            return result
        except Exception as e:
            logger.warning(f"⚠️ 第{group_index}组提取失败：{e}")
            return ""
    
    # 并发执行（限制并发数为 3）
    semaphore = asyncio.Semaphore(3)
    
    async def limited_process(idx, group):
        async with semaphore:
            return await run_in_threadpool(lambda: process_group(idx, group))
    
    tasks = [limited_process(i, g) for i, g in enumerate(grouped_chunks)]
    partial_results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # 过滤失败结果
    valid_results = [r for r in partial_results if isinstance(r, str) and r.strip()]
    logger.info(f"   ✅ Map 阶段完成：成功 {len(valid_results)}/{len(grouped_chunks)} 组")
    
    # 2. Reduce 阶段：全局整合去重
    all_maps_text = "\n\n".join(valid_results)
    
    reduce_prompt = f"""以下是从一篇长文中不同部分提取的零散知识点列表。

【原始提取结果】：
{all_maps_text}

【任务要求】：
请将其整合成一份系统的【考核大纲】，要求：
1. 模块化：合并重复和相关的点，形成独立的核心模块
2. 结构化：每个模块包含 module_name（模块名称）和 key_points
3. 系统化：按照逻辑顺序排列，从基础到高级

请严格以 JSON 格式输出：
{{
  "knowledge_modules": [
    {{
      "module_name": "模块名称",
      "key_points": ["关键点 1", "关键点 2", "关键点 3",...]
    }}
  ]
}}
"""
    
    try:
        final_map = call_llm(
            reduce_prompt,
            "你是一个教育学专家，擅长设计系统化的考核大纲。",
            config,
            model_index=model_index
        )
        final_map_json = extract_json_from_llm_response(final_map)
        modules = final_map_json.get("knowledge_modules", [])
        logger.info(f"   ✅ Reduce 阶段完成：整合为 {len(modules)} 个核心模块")
        return final_map_json
    except Exception as e:
        logger.error(f"❌ Reduce 阶段失败：{e}")
        # 兜底：返回空列表
        return {"knowledge_modules": []}


@app.post("/api/generate")
async def generate_questions(req: GenerateRequest):
    article_text = req.article_text.strip()
    if len(article_text) < 100:
        raise HTTPException(status_code=400, detail="文章内容太短，请输入至少 100 字")

    word_count = len(article_text)
    # 根据文章长度动态调整题目数量（三个层级）
    if word_count < 1000:
        basic_q, apply_q, advanced_q = 3, 2, 1
    elif word_count < 3000:
        basic_q, apply_q, advanced_q = 5, 3, 2
    elif word_count < 5000:
        basic_q, apply_q, advanced_q = 7, 4, 3
    else:
        basic_q, apply_q, advanced_q = 9, 6, 4

    config = req.api_config or load_config()

    # 【RAG 增强出题】先对文章进行语义切片并向量化，然后检索与三层级题目结构最相关的分块
    logger.info(f"🔍 开始 RAG 增强出题流程...")
    
    # 1. 文本切片
    chunks = semantic_chunking(article_text, chunk_size=500, overlap=50)
    logger.info(f"📝 切分为 {len(chunks)} 个语义块")
    
    # 🔑 获取前端指定的分阶段模型索引
    extract_model_idx = req.extract_model_index
    generate_model_idx = req.generate_model_index
    
    # 1. Map-Reduce 阶段 (如果需要)
    knowledge_modules = []
    if word_count > 2000:
        try:
            # 使用 extract_model_idx
            map_result = await extract_knowledge_map(chunks, config, model_index=extract_model_idx)
            knowledge_modules = map_result.get("knowledge_modules", [])
            logger.info(f"   ✅ 提取到 {len(knowledge_modules)} 个核心知识模块")
        except Exception as e:
            logger.warning(f"⚠️ 知识图谱提取失败：{e}，使用传统方式出题")
    
    # 2. 批量获取向量
    try:
        batch_size = 64
        all_embeddings = []
        
        for i in range(0, len(chunks), batch_size):
            batch_chunks = chunks[i:i + batch_size]
            batch_texts = [chunk['text'] for chunk in batch_chunks]
            logger.info(f"   正在向量化第 {i//batch_size + 1} 批 ({len(batch_texts)} 条)...")
            
            embeddings = await get_embeddings(batch_texts, dimensions=1024)
            all_embeddings.extend(embeddings)
            
            if i + batch_size < len(chunks):
                await asyncio.sleep(0.5)
        
        # 3. 将向量与切片合并
        for idx, chunk in enumerate(chunks):
            chunk['embedding'] = all_embeddings[idx]
        
        # 4. 临时保存到 MongoDB（用于后续检索）
        temp_session_id = f"temp_{str(uuid.uuid4())[:8]}"
        save_article_chunks(temp_session_id, chunks)
        logger.info(f"   ✅ 临时向量索引创建完成")
        
        # 5. 🔑 新方案：基于知识模块的定向取证 (Module-Centric RAG)
        logger.info(f"🎯 开始基于 {len(knowledge_modules)} 个核心模块进行定向取证...")
        
        module_contexts = {} # 存储每个模块对应的原文分块
        
        for module in knowledge_modules:
            module_name = module.get("module_name", "")
            key_points = module.get("key_points", [])
            
            if not module_name: continue

            # 构造针对该模块的强语义查询，消除语义稀释
            query_text = f"{module_name}：{'、'.join(key_points)}"
            
            try:
                # 获取该模块的查询向量
                query_emb = await get_embeddings([query_text], dimensions=1024)
                
                # 检索该模块最相关的 Top-K 分块 (建议 K=4，保证信息密度)
                search_result = search_similar_chunks(
                    session_id=temp_session_id,
                    query_embedding=query_emb[0],
                    top_k=4 
                )
                
                chunks = search_result.get("evidence_for_display", [])
                module_contexts[module_name] = chunks
                
                logger.info(f"✅ 模块【{module_name}】匹配到 {len(chunks)} 个核心片段")
                
            except Exception as e:
                logger.warning(f"⚠️ 模块【{module_name}】检索失败：{e}")
                module_contexts[module_name] = []

        # 6. 构建高信息密度的 Prompt (Prompt 瘦身)
        context_enhancement = "\n\n【模块化核心依据】以下是各知识模块对应的原文关键段落：\n"
        
        for module in knowledge_modules:
            m_name = module.get("module_name", "")
            chunks = module_contexts.get(m_name, [])
            
            context_enhancement += f"\n### 模块：{m_name}\n"
            context_enhancement += f"关键点：{', '.join(module.get('key_points', []))}\n"
            
            if chunks:
                context_enhancement += "相关原文：\n"
                for c in chunks:
                    # 记录 chunk_index 以便后续证据回溯
                    context_enhancement += f"[Chunk-{c.get('chunk_index', 0)}] {c['text']}\n"
            else:
                context_enhancement += "(未检索到强相关原文，请根据全文作答)\n"
        
    except Exception as e:
        logger.warning(f"⚠️ RAG 增强出题失败：{e}，使用原始文章文本")
        context_enhancement = ""
    
    # 使用三层级认知模型的出题策略
    system_prompt = """你是国家级学力测试命题官，基于布鲁姆教育目标分类法设计三层级考核题目。
你必须严格以 JSON 格式输出，确保题目质量、区分度和考核效度。"""

    prompt = f"""请根据以下文章内容，设计一套完整的三层级考核试卷。

【文章内容】：
{article_text}
{context_enhancement}
{'''【知识模块清单】以下是文章的核心知识模块，请确保题目全面覆盖每个模块：\n''' + json.dumps(knowledge_modules, ensure_ascii=False, indent=2) if knowledge_modules else ''}

【命题核心指令 - 逻辑闭环】：
你必须严格按照以下【模块化配额】进行出题，确保每个模块都有题目覆盖，且难度呈梯度分布：

对于每一个【知识模块】，请至少生成 2-3 道题目，必须包含：
1. **基础认知题 (Basic)**：考察该模块下的核心定义、专有名词或关键数据。
2. **应用分析题 (Apply)**：结合该模块的原理，设计一个实际场景或案例分析。
3. **高阶思维题 (Advanced)**：(可选) 针对该模块的局限性、与其他模块的关联或未来延伸进行提问。

【证据回溯要求】：
在生成每道题时，你必须从【模块化核心依据】中引用对应的 [Chunk-X] 作为出题依据。
在输出的 JSON 中，每道题必须包含一个字段 "source_chunk_indices": [X, Y]，记录参考了哪几个分块。

【出题质量要求】：
1. 每道题必须标注 cognitive_level 字段："basic" / "apply" / "advanced"
2. 每道题必须标注 question_type 字段：具体题型名称
3. 选择题的干扰项要基于学生常见误解设计，而且选择题question字段不许出现options选项内容。
4. 【主观题答案深度约束】：`answer` 字段必须以“标准参考范文”的形式呈现，要求逻辑严密、行文连贯、一气呵成。
   - 禁止出现：[要点 1]、[逻辑重组]、①②③、标准参考范文、分析要点和解决方案等明显的结构化标签在 `answer` 当中。
   - 必须包含：答案应是一个完整的逻辑段落，自然地嵌入文中所有核心 [专有名词] 和 [关键数据]。
   - 内容结构：应先正面回答核心问题，随后深入阐述其背后的 [原理/成因]，并引用原文逻辑进行严丝合缝的论证，最后进行总结或延伸。
   - 语义密度：答案必须像一篇高质量的小论文，确保学生即便只答出其中一部分逻辑，也能在语义空间中与该范文产生高相似度。
5. 【评分细则分离】：将原本的结构化拆解放入 `grading_rubric` 字段。
   - `grading_rubric` 应详细列出：得分点、关键词扣减标准、逻辑完整性评估。这样实现了“参考答案（给学生看/做对比）”与“评分标准（给系统看）”的分离。
6. 所有题目都要有标准答案和详细解析
7. 题目难度要有梯度，从识记→理解→应用→分析→评价→创造
8. 🔑【高信息密度题型】优先使用复合题型提高覆盖率：
   - 多空填空题：一道填空题挖 3-5 个空，涵盖一个完整流程
   - 综合案例分析：构建一个案例，要求同时用到多个模块的知识
   - 多选题融合：将相关的两个知识点融合在一道多选题中
9. 【题目文本格式化】为了让题目在前端展示时层次分明，请在 question 字段中使用以下格式：
   - 分隔不同部分不要在题目中使用 <br/> 标签。请直接使用标准换行符 \n。前端将通过 CSS 的 white-space: pre-wrap 自动处理换行
   - 对于需要列举的内容，使用数字序号或项目符号
10. 【填空题特别要求】：
- 必须在 question 字段中使用 ___（三个下划线）作为占位符。如果是多个空格，请在 question 中按顺序放置多个 ___。
- 挖空处必须是【专有名词】、【核心定义】或【数据指标】
11.【语义密度强制指令】：主观题的 `answer` 字段语义必须覆盖对应原文分块 80% 以上的关键信息点。
12.如果你生成的 answer 字段少于 500 字，我将视为命题失败并要求重新生成。

【JSON 输出格式】（不要添加任何其他文字）：
{{
  "title": "文章标题（从内容中提取或概括，不超过 20 字）",
  "summary": "文章核心摘要（50-100 字，用于 AI 审题）",
  "questions": [
    {{
      "id": 1,
      "cognitive_level": "basic",
      "question_type": "fill_blank",
      "question": "题干内容（填空题用下划线 ___ 表示空白处）",
      "answer": "标准答案",
      "explanation": "答案解析（说明考查的知识点）",
      "score_weight": 1.0
    }},
    {{
      "id": 2,
      "cognitive_level": "basic",
      "question_type": "true_false",
      "question": "题干内容",
      "answer": "正确/错误",
      "explanation": "答案解析（指出逻辑陷阱在哪里）",
      "score_weight": 1.0
    }},
    {{
      "id": 3,
      "cognitive_level": "basic",
      "question_type": "single_choice",
      "question": "题干内容",
      "options": {{"A": "选项 A", "B": "选项 B", "C": "选项 C", "D": "选项 D"}},
      "answer": "A",
      "explanation": "答案解析（说明为什么选这个，其他选项为什么错）",
      "score_weight": 1.0
    }},
    {{
      "id": 4,
      "cognitive_level": "apply",
      "question_type": "multiple_choice",
      "question": "题干内容（多选题）",
      "options": {{"A": "选项 A", "B": "选项 B", "C": "选项 C", "D": "选项 D"}},
      "answer": "AB",
      "explanation": "答案解析（说明每个选项的对错理由）",
      "score_weight": 1.5
    }},
    {{
      "id": 5,
      "cognitive_level": "apply",
      "question_type": "short_answer",
      "question": "题干内容",
      "answer": "f 字符串作为 Python 3.6 引入的格式化语法，其核心作用是通过在字符串前缀加'f'并利用花括号嵌入变量，实现了数据与展示层的高度集成。从运行机制上看，f 字符串不同于传统的 format 方法或百分号占位符，它在编译时而非运行时求值，这显著提升了程序的执行效率。同时，它支持在花括号内直接进行逻辑运算和函数调用，极大增强了代码的可读性与维护性，是现代 Python 开发中推荐的字符串处理标准方案。",
      "explanation": "本题考查对 f 字符串编译原理及应用场景的深度理解。",
      "grading_rubric": "1. 准确提到‘编译时求值’得 0.5 分；2. 提到‘变量嵌入及语法简洁性’得 0.5 分；3. 提到‘支持表达式/运算’得 0.5 分。",
      "evidence_source": "直接摘录原文中支撑本题答案的句子或段落（字数需完整，确保逻辑闭环）",
      "score_weight": 1.5
    }},
    {{
      "id": 6,
      "cognitive_level": "apply",
      "question_type": "case_analysis",
      "question": "案例描述 + 分析问题",
      "answer": "分析要点和解决方案",
      "explanation": "答案解析",
      "grading_rubric": "评分参考：问题识别 X 分，理论应用 Y 分，方案合理性 Z 分...",
      "evidence_source": "直接摘录原文中支撑本题答案的句子或段落（字数需完整，确保逻辑闭环）",
      "score_weight": 2.0
    }},
    {{
      "id": 7,
      "cognitive_level": "advanced",
      "question_type": "open_ended",
      "question": "开放式探究问题",
      "answer": "参考答案方向（开放性答案，不设唯一标准）",
      "explanation": "评价指引",
      "grading_rubric": "评分维度：观点深度、论证逻辑、创新性、与文章关联度",
      "evidence_source": "直接摘录原文中支撑本题答案的句子或段落（字数需完整，确保逻辑闭环）",
      "score_weight": 2.0
    }},
    {{
      "id": 8,
      "cognitive_level": "advanced",
      "question_type": "debate",
      "question": "给出对立观点，要求反驳或支持",
      "answer": "参考论据和论证思路",
      "explanation": "评价指引",
      "grading_rubric": "评分维度：论据引用、逻辑严密性、批判性思维",
      "evidence_source": "直接摘录原文中支撑本题答案的句子或段落（字数需完整，确保逻辑闭环）",
      "score_weight": 2.0
    }},
    {{
      "id": 9,
      "cognitive_level": "advanced",
      "question_type": "design",
      "question": "设计任务描述",
      "answer": "设计框架示例",
      "explanation": "评价指引",
      "grading_rubric": "评分维度：创新性、可行性、与文章价值观一致性、完整性",
      "evidence_source": "直接摘录原文中支撑本题答案的句子或段落（字数需完整，确保逻辑闭环）",
      "score_weight": 2.5
    }}
  ]
}}"""

    try:
        # 🔑 在 call_llm 中指定 generate_model_idx
        result = call_llm(prompt, system_prompt, config, model_index=generate_model_idx)
        questions_data = extract_json_from_llm_response(result)
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="AI 生成的题目格式异常，请重试")

    questions = questions_data.get("questions", [])
    if not questions:
        raise HTTPException(status_code=500, detail="AI 未能生成有效题目，请重试")

    # 验证题目是否包含三层级结构
    levels = set(q.get("cognitive_level") for q in questions)
    if not levels.issubset({"basic", "apply", "advanced"}):
        logger.info(f"⚠️ 题目认知层级标注不完整，尝试自动修复...")
        # 为没有标注的题目默认分配层级
        for i, q in enumerate(questions):
            if "cognitive_level" not in q:
                if i < basic_q:
                    q["cognitive_level"] = "basic"
                elif i < basic_q + apply_q:
                    q["cognitive_level"] = "apply"
                else:
                    q["cognitive_level"] = "advanced"
            if "question_type" not in q:
                q["question_type"] = "unknown"
            if "score_weight" not in q:
                q["score_weight"] = 1.0

    # 【关键步骤】通过向量检索为每道题自动填充 evidence_source
    # 出题时立即检索并存储原文依据，评分时直接使用，避免重复检索
    logger.info(f"🔍 开始通过向量检索填充原文依据...")
    
    for i, q in enumerate(questions):
        try:
            # 使用题目的问题（question）或标准答案（answer）作为检索词
            search_query = f"{q.get('question', '')} {q.get('answer', '')}".strip()
            
            if not search_query:
                logger.warning(f"⚠️ 题目 {i+1} 的问题和答案均为空，跳过检索")
                q["evidence_source"] = "题目内容为空，无法检索"
                continue
            
            # 获取题目 + 答案的联合向量表示
            question_embedding = await get_embeddings([search_query], dimensions=1024)
            
            # 搜索最相关的分块（top_k=1，获取相似度最高的一个）
            search_result = search_similar_chunks(
                session_id=temp_session_id,
                query_embedding=question_embedding[0],
                top_k=1
            )
            
            evidence_for_display = search_result.get("evidence_for_display", [])
            
            if evidence_for_display and len(evidence_for_display) > 0:
                # 取相似度最高的一个分块内容作为 evidence_source
                best_hit = evidence_for_display[0]
                q["evidence_source"] = best_hit.get("text", "未找到明确原文依据")
                similarity = best_hit.get("similarity_pct", 0)
                logger.info(f"   ✅ 题目 {i+1} ({q.get('question_type', 'unknown')}) 检索到证据 (相似度：{similarity:.1f}%)")
            else:
                q["evidence_source"] = "检索未命中原文"
                logger.warning(f"   ⚠️ 题目 {i+1} 检索未命中任何原文分块")
                
        except Exception as e:
            logger.error(f"❌ 题目 {i+1} 的证据检索失败：{e}")
            q["evidence_source"] = f"检索出错：{str(e)}"
    
    logger.info(f"✅ 所有题目的原文依据填充完成")

    session_id = str(uuid.uuid4())[:8]
    session_data = {
        "session_id": session_id,
        "title": questions_data.get("title", "未命名文章"),
        "summary": questions_data.get("summary", ""),
        "article_text": article_text,
        "questions": questions,
        "answers": {},
        "pass_score": req.pass_score,
        "created_at": datetime.now().isoformat(),
        "status": "in_progress",
        "stats": {
            "basic_count": len([q for q in questions if q.get("cognitive_level") == "basic"]),
            "apply_count": len([q for q in questions if q.get("cognitive_level") == "apply"]),
            "advanced_count": len([q for q in questions if q.get("cognitive_level") == "advanced"]),
        }
    }
    save_session(session_data, session_id)
    
    # 新增：将临时向量索引转移到正式 session（避免重复向量化）
    try:
        logger.info(f"🔄 转移向量索引到正式 session {session_id}...")
        # 从临时 session 读取
        from database import get_chunks_by_session
        existing_chunks = get_chunks_by_session(temp_session_id)
        
        if existing_chunks:
            # 更新 session_id 并保存到新 session
            for chunk in existing_chunks:
                chunk['session_id'] = session_id
            
            # 批量保存到正式 session
            col = get_chunks_collection()
            col.delete_many({"session_id": session_id})  # 先清理
            for chunk in existing_chunks:
                col.insert_one(chunk)
            
            # 删除临时索引
            from database import delete_session_chunks
            delete_session_chunks(temp_session_id)
            
            logger.info(f"✅ 向量索引已转移到正式 session")
        else:
            # 如果之前失败了，重新创建
            raise Exception("临时索引不存在")
            
    except Exception as e:
        logger.warning(f"⚠️ 转移失败或需要重新创建：{e}")
        # 重新执行向量化流程

    return {
        "session_id": session_id,
        "title": session_data["title"],
        "summary": session_data["summary"],
        "questions": questions,
        "total": len(questions),
        "stats": session_data["stats"],
    }


@app.post("/api/grade")
async def grade_exam(req: GradeRequest):
    evidence_for_display = None
    # 1. 从数据库读取原始会话（仅读取一次）
    session = await run_in_threadpool(load_session, req.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话已过期或不存在")

    # 优先使用数据库存储的题目，确保 answer 字段存在
    db_questions = session.get("questions", [])
    user_answers = req.answers
    pass_score = req.pass_score
    config = req.api_config or load_config()

    if not db_questions:
        raise HTTPException(status_code=500, detail="该会话中没有题目数据")

    # 计算总分和权重 - 确保 score_weight 转换为浮点数
    total_weight = sum(float(q.get("score_weight", 1.0)) for q in db_questions)
    points_per_weight = 100 / total_weight

    results = []
    total_earned = 0.0
    level_scores = {"basic": 0.0, "apply": 0.0, "advanced": 0.0}
    level_max = {"basic": 0.0, "apply": 0.0, "advanced": 0.0}

    # 2. 遍历题目评分 - 基于认知层级采用不同评分策略
    for q in db_questions:
        qid = str(q["id"])
        cognitive_level = q.get("cognitive_level", "basic")
        question_type = q.get("question_type", "unknown")
        user_ans = user_answers.get(qid, "").strip()
        standard_answer = q.get("answer", "未提供标准答案").strip()
        score_weight = float(q.get("score_weight", 1.0))  # 确保转换为浮点数
        max_score = points_per_weight * score_weight

        # 累计该层级的满分
        level_max[cognitive_level] += max_score

        # --- 客观题评分（填空题、判断题、单选题、多选题）---
        if question_type in ["fill_blank", "true_false", "single_choice", "multiple_choice"]:
            # 客观题评分逻辑增强
            if question_type == "fill_blank":
                # 处理答案格式：支持 | 或 , 作为分隔符
                standard_parts = [p.strip().upper() for p in standard_answer.replace(',', '|').split('|') if p.strip()]
                user_parts = [p.strip().upper() for p in user_ans.replace(',', '|').replace('||', '|').split('|') if p.strip()]
        
                total_blanks = len(standard_parts)
                if total_blanks > 0:
                    score_per_blank = max_score / total_blanks
                    correct_count = 0
                    # 对位比对
                    for i in range(min(len(standard_parts), len(user_parts))):
                        if user_parts[i] == standard_parts[i]:
                            correct_count += 1
                    earned = score_per_blank * correct_count
                else:
                    earned = 0.0
            elif question_type == "multiple_choice":
                # 多选题评分规则：
                # 1. 全对：只选择所有正确答案，不多选也不少选 → 100% 分数
                # 2. 部分对：选择了部分正确答案（且没有选错误答案） → 50% 分数
                # 3. 全错：选了错误答案或空选 → 0 分

                # 清理并标准化答案格式
                correct_str = standard_answer.upper().replace(' ', '').replace(',', '')
                user_str = user_ans.upper().replace(' ', '').replace(',', '')

                # 转换为集合进行比较
                correct_set = set(correct_str)
                user_set = set(user_str)

                # 过滤掉空字符
                correct_set = {c for c in correct_set if c.isalpha()}
                user_set = {c for c in user_set if c.isalpha()}

                # 检查是否选了错误答案（用户选的不在正确答案中）
                wrong_selections = user_set - correct_set

                if len(wrong_selections) > 0:
                    # 选了至少一个错误答案 → 0 分
                    earned = 0.0
                elif len(user_set) == 0:
                    # 空选 → 0 分
                    earned = 0.0
                elif user_set == correct_set:
                    # 完全匹配 → 100% 分数
                    earned = max_score
                elif user_set < correct_set:
                    # 真子集：选了部分正确答案，且没有选错误答案 → 50% 分数
                    earned = max_score * 0.5
                else:
                    # 其他情况
                    earned = 0.0
            else:
                # 其他客观题：二元评分
                correct = user_ans.upper().strip() == standard_answer.upper().strip()
                earned = max_score if correct else 0.0

            total_earned += earned
            level_scores[cognitive_level] += earned

            # 构建返回结果
            result_item = {
                "id": q["id"],
                "cognitive_level": cognitive_level,
                "question_type": question_type,
                "question": q["question"],
                "correct_answer": standard_answer,
                "user_answer": user_ans,
                "is_correct": earned > 0,
                "score": round(earned, 1),
                "max_score": round(max_score, 1),
                "explanation": q.get("explanation", ""),
            }

            # 为选择题添加 option_details
            if question_type in ["single_choice", "multiple_choice"]:
                options = q.get("options", {})
                correct_set = set(standard_answer.upper().replace(',', '').replace(' ', ''))
                user_set = set(user_ans.upper().replace(',', '').replace(' ', ''))

                option_details = []
                for key, value in options.items():
                    is_correct = key.upper() in correct_set
                    is_user_selected = key.upper() in user_set

                    # 判断状态
                    if is_correct and is_user_selected:
                        status = "correctly_selected"  # 选对了
                    elif not is_correct and is_user_selected:
                        status = "incorrectly_selected"  # 选错了
                    elif is_correct and not is_user_selected:
                        status = "missed"  # 漏选了
                    else:
                        status = "not_selected"  # 正常未选

                    option_details.append({
                        "key": key,
                        "value": value,
                        "is_correct": is_correct,
                        "is_user_selected": is_user_selected,
                        "status": status
                    })

                result_item["options"] = options
                result_item["option_details"] = option_details

            results.append(result_item)

        # --- 主观题评分（简答题、案例分析、开放式问答、辩论题、设计题）---
        else:
            if not user_ans:
                earned, comment = 0.0, "考生未作答，计 0 分。"
                extra_info = {}
            # --- 新增：增加对无效回答的强制检查 ---
            elif user_ans.lower().strip() in ["不知道", "不清楚", "not sure", "no idea", "idk", ""]:
                earned, comment = 0.0, f'考生回答为"{user_ans}"，视为无效回答，计 0 分。'
                extra_info = {}
            # --- 新增结束 ---
            else:
                try:
                    # 【优化】优先使用出题时预存的 evidence_source，无需重新检索
                    # 出题时已经通过向量检索填充了该字段，直接用于评分上下文
                    evidence_source = q.get("evidence_source", "")
                                
                    if evidence_source:
                        # 直接使用预存证据进行评分
                        context_str = evidence_source
                        # 将单个字符串转换为列表格式以便前端展示
                        evidence_for_display = [{
                            "text": evidence_source,
                            "similarity_pct": 100.0,  # 预存证据视为 100% 相关
                            "chunk_index": 0
                        }]
                        logger.info(f"✅ 题目 {qid} 使用预存原文依据（{len(evidence_source)}字）")
                    else:
                        # 兜底方案：如果没有预存证据，则使用 RAG 检索
                        logger.warning(f"⚠️ 题目 {qid} 缺少 evidence_source，启用 RAG 检索兜底")
                        try:
                            # 获取题目 + 标准答案的联合向量表示
                            question_with_answer = f"{q['question']} {standard_answer}"
                            question_embedding = await get_embeddings([question_with_answer], dimensions=1024)
                                        
                            # 搜索最相关的分块
                            search_result = search_similar_chunks(
                                session_id=req.session_id,
                                query_embedding=question_embedding[0],
                                top_k=5
                            )
                                        
                            context_for_llm = search_result.get("context_for_llm", [])
                            evidence_for_display = search_result.get("evidence_for_display", [])
                            context_str = "\n\n".join([chunk['text'] for chunk in context_for_llm])
                            logger.info(f"📚 题目 {qid} RAG 兜底检索到 {len(context_for_llm)} 个相关段落")
                                        
                        except Exception as e:
                            logger.info(f"⚠️  RAG 兜底检索失败：{e}")
                            context_str = ""
                            evidence_for_display = []
                    
                    # 根据认知层级和题型定制评分 prompt
                    grading_rubric = q.get("grading_rubric", "")

                    if cognitive_level == "basic":
                        # 基础层级：侧重知识点覆盖
                        grade_prompt = f"""你是一个冷酷、严谨的学术评阅官，严禁给予任何'辛苦分'或'态度分'。请对以下【基础认知类】题目评分。

【题型】：{question_type}
【题目】：{q['question']}
【标准答案】：{standard_answer}
【学生答案】：{user_ans}
【满分】：{round(max_score, 1)}
{f'【评分参考】：{grading_rubric}' if grading_rubric else ''}
{f'''【原文相关证据】：
{context_str}

'''.strip() if context_str else ''}

评分原则：
1. 关键词匹配：答案中是否包含标准答案的核心术语
2. 概念准确性：对定义的表述是否准确
3. 事实正确性：是否有明显的知识性错误
{'''4. 原文核对：学生答案必须能在原文中找到依据，如果原文没有提到该观点，即使逻辑自洽也需酌情扣分。'''.strip() if context_str else ''}

强制零分规则：
- 如果学生回答“不知道”、“不清楚”、"not sure"、"no idea"、"idk"或语义完全无关，必须给 0 分。

请严格返回 JSON: {{"score": 分值 (数字), "comment": "具体评语 (指出对错点)", "match_rate": 0-100 的数字}}"""

                    elif cognitive_level == "apply":
                        # 应用层级：侧重分析能力和理论应用
                        grade_prompt = f"""你是冷酷、严谨的逻辑学与案例评阅专家。请对以下【应用与分析类】题目评分。

【题型】：{question_type}
【题目】：{q['question']}
【参考答案要点】：{standard_answer}
【学生答案】：{user_ans}
【满分】：{round(max_score, 1)}
{f'【评分细则】：{grading_rubric}' if grading_rubric else ''}
{f'''【原文相关证据】：
{context_str}

'''.strip() if context_str else ''}

评分维度：
1. 问题识别：是否准确识别出案例中的关键问题
2. 理论应用：是否正确运用文中理论进行分析
3. 逻辑严密性：论证过程是否清晰、有说服力
4. 方案可行性：提出的解决方案是否合理可行
{'''5. 原文关联：分析必须基于原文提供的证据，不能脱离原文空谈。'''.strip() if context_str else ''}

如果学生回答与题目无关或仅复述题干，得分不超过 30%。
强制零分规则：
- 如果学生回答“不知道”、“不清楚”、"not sure"、"no idea"、"idk"或语义完全无关，必须给 0 分。

请严格返回 JSON: {{"score": 分值，"comment": "针对性评语 (指出优点和不足)", "analysis_quality": "high/medium/low"}}"""

                    else:  # advanced
                        # 高阶层级：侧重批判性思维和创新性
                        grade_prompt = f"""你是冷酷、严谨的批判性思维与创新评价专家。请对以下【高阶思维类】题目评分。

【题型】：{question_type}
【题目】：{q['question']}
【参考方向】：{standard_answer}
【学生答案】：{user_ans}
【满分】：{round(max_score, 1)}
{f'【评价维度】：{grading_rubric}' if grading_rubric else ''}
{f'''【原文相关证据】：
{context_str}

'''.strip() if context_str else ''}

评分维度：
1. 观点深度：是否有独到见解，超越表面理解
2. 论证质量：论据是否充分，逻辑是否严密
3. 批判性思维：是否能辩证分析问题，考虑多角度
4. 创新性：是否有新颖的观点或创造性的解决方案
5. 与文章关联：是否能有效引用或延伸文章观点
{'''6. 证据支持：观点必须能在原文中找到支撑或与原文逻辑一致。'''.strip() if context_str else ''}

鼓励创新思维，即使与参考答案不同，只要论证有力也应给高分。
强制零分规则：
- 如果学生回答“不知道”、“不清楚”、"not sure"、"no idea"、"idk"或语义完全无关，必须给 0 分。

请严格返回 JSON: {{"score": 分值，"comment": "发展性评语 (指出思维亮点和改进建议)", "thinking_level": "high/medium/low"}}"""

                    # 使用 run_in_threadpool 防止同步 call_llm 阻塞异步循环
                    grade_result = await run_in_threadpool(call_llm, grade_prompt,
                        "你是一个专业化、结构化的智能评分系统", config)

                    # 鲁棒的 JSON 提取逻辑
                    json_match = re.search(r'\{.*\}', grade_result, re.DOTALL)
                    if json_match:
                        gd = json.loads(json_match.group())
                        raw_score = float(gd.get("score", 0))
                        earned = max(0.0, min(raw_score, max_score))
                        comment = gd.get("comment", "评分完成")

                        # 记录 AI 生成的额外评价信息
                        extra_info = {}
                        if "analysis_quality" in gd:
                            extra_info["analysis_quality"] = gd["analysis_quality"]
                        if "thinking_level" in gd:
                            extra_info["thinking_level"] = gd["thinking_level"]
                        if "match_rate" in gd:
                            extra_info["match_rate"] = gd["match_rate"]
                    else:
                        raise ValueError("未能识别到 JSON 格式")

                except Exception as e:
                    logger.info(f"⚠️ {cognitive_level} 层级题目 {qid} 评分异常：{e}")
                    earned, comment = 0.0, "评分解析失败，请检查答案格式。"
                    extra_info = {}

            total_earned += earned
            level_scores[cognitive_level] += earned
            results.append({
                "id": q["id"],
                "cognitive_level": cognitive_level,
                "question_type": question_type,
                "question": q["question"],
                "correct_answer": standard_answer,
                "user_answer": user_ans,
                "score": round(earned, 1),
                "max_score": round(max_score, 1),
                "comment": comment,
                "explanation": q.get("explanation", ""),
                "evidence": [
                    {
                        "text": chunk.get('text', chunk) if isinstance(chunk, dict) else chunk,
                        "similarity": round(chunk.get('similarity_pct', 100.0), 1) if isinstance(chunk, dict) else 100.0,
                        "level": _get_evidence_level(chunk.get('similarity_pct', 100.0)) if isinstance(chunk, dict) else "极强相关"
                    }
                    for chunk in evidence_for_display
                ] if evidence_for_display else [],
                **extra_info
            })

    # 3. 最终得分计算
    final_score = round(total_earned, 1)
    passed = final_score >= pass_score

    # 计算各层级的得分率
    level_rates = {}
    for level in ["basic", "apply", "advanced"]:
        if level_max[level] > 0:
            level_rates[level] = round(level_scores[level] / level_max[level] * 100, 1)
        else:
            level_rates[level] = 0.0

    # 4. 更新并保存会话状态
    session.update({
        "status": "graded",
        "score": final_score,
        "passed": passed,
        "graded_at": datetime.now().isoformat(),
        "details": results,
        "answers": user_answers,
        "level_scores": level_scores,
        "level_max": level_max,
        "level_rates": level_rates,
    })

    await run_in_threadpool(save_session, session, req.session_id)

    return {
        "score": final_score,
        "passed": passed,
        "pass_score": pass_score,
        "details": results,
        "level_analysis": {
            "basic": {"score": level_scores["basic"], "max": level_max["basic"], "rate": level_rates["basic"]},
            "apply": {"score": level_scores["apply"], "max": level_max["apply"], "rate": level_rates["apply"]},
            "advanced": {"score": level_scores["advanced"], "max": level_max["advanced"], "rate": level_rates["advanced"]},
        }
    }


@app.post("/api/save")
async def save_progress(req: SaveProgressRequest):
    session = load_session(req.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    session["answers"] = req.answers
    session["status"] = "saved"
    session["updated_at"] = datetime.now().isoformat()
    save_session(session, req.session_id)
    return {"message": "进度已保存"}


@app.get("/api/load/{session_id}")
async def load_progress(session_id: str):
    session = load_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    return session


@app.get("/api/sessions")
async def list_sessions():
    """从 MongoDB 查询所有会话，按创建时间倒序"""
    from database import list_all_sessions
    sessions_raw = list_all_sessions(limit=100)
    sessions = []
    for doc in sessions_raw:
        sessions.append({
            "session_id": doc.get("session_id", ""),
            "title": doc.get("title", ""),
            "status": doc.get("status", ""),
            "created_at": doc.get("created_at", ""),
            "score": doc.get("score"),
            "total": len(doc.get("questions", [])),
            "answered": len([a for a in doc.get("answers", {}).values() if a]),
        })
    return sessions


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    """从 MongoDB 中物理删除指定的会话记录"""
    from database import delete_exam_session
    success = delete_exam_session(session_id)
    
    if not success:
        raise HTTPException(status_code=404, detail="记录不存在")
    
    return {"status": "success"}


@app.get("/api/fetch-url")
async def fetch_url(url: str):
    try:
        # 1. 从 database.py 导入并执行同步的抓取逻辑
        logger.info(f"🌐 正在抓取：{url}")
        content = await run_in_threadpool(fetch_article_from_url, url)

        # 2. 调用异步并发清洗（需要从 database.py 导入 clean_scraping_text 和添加新的异步清洗函数）
        logger.info(f"✨ 正在并发清洗文本...")
        # 注意：这里暂时保留原有逻辑，后续可以将 clean_content_with_llm_concurrent 也移到 database.py
        final_content = await clean_content_with_llm_concurrent(content)

        return {
            "content": final_content,
            "word_count": len(final_content)
        }
    except HTTPException as he:
        raise he
    except Exception as e:
        logger.info(f"❌ 接口执行异常：{e}")
        raise HTTPException(status_code=500, detail=str(e))


# --- Static Files & Index ---
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


if __name__ == "__main__":
    logger.info("=" * 50)
    logger.info("文章考核系统已启动！")
    logger.info("请在浏览器访问：http://127.0.0.1:8000")
    logger.info("=" * 50)
    # 启动时预连接 MongoDB，提前暴露连接问题
    try:
        get_collection()
    except Exception:
        logger.info("MongoDB 未连接，系统将在首次请求时重试")
    uvicorn.run(app, host="0.0.0.0", port=8000)
