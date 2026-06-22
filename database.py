# database.py
import os
import re
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError
from datetime import datetime
from typing import Optional, List
import logging
import socket
import subprocess
import time
from playwright.sync_api import sync_playwright

# MongoDB 配置
MONGO_HOST = "localhost"
MONGO_PORT = 27017
MONGO_DB = "article_exam_web"
MONGO_COLLECTION = "examination"
MONGO_CHUNKS_COLLECTION = "article_chunks"

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class Database:
    """封装 MongoDB 操作的单例类"""
    _instance = None
    _client = None
    _db = None
    _collection = None
    _chunks_collection = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def connect(self):
        if Database._collection is None:
            try:
                Database._client = MongoClient(
                    host=MONGO_HOST,
                    port=MONGO_PORT,
                    serverSelectionTimeoutMS=5000,
                )
                Database._client.admin.command("ping")
                Database._db = Database._client[MONGO_DB]
                Database._collection = Database._db[MONGO_COLLECTION]
                Database._chunks_collection = Database._db[MONGO_CHUNKS_COLLECTION]

                # 初始化索引
                Database._collection.create_index("session_id", unique=True, sparse=True)
                Database._collection.create_index("created_at", unique=True, sparse=True)
                
                # 向量索引相关（MongoDB 8.2+ 支持）
                Database._chunks_collection.create_index([("session_id", 1), ("chunk_index", 1)])
                # 注意：向量索引需要在 MongoDB Atlas 或配置后手动创建

                logger.info(f"✅ MongoDB 已连接：{MONGO_HOST}:{MONGO_PORT}/{MONGO_DB}")
                logger.info(f"   主集合：{MONGO_COLLECTION}, 切片集合：{MONGO_CHUNKS_COLLECTION}")
            except Exception as e:
                logger.error(f"❌ MongoDB 初始化失败：{e}")
                Database._collection = None
                raise

    def get_collection(self):
        if Database._collection is None:
            self.connect()
        return Database._collection


# --- 工具函数 ---

def is_port_open(port: int) -> bool:
    """检查本地端口是否已被占用"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(('127.0.0.1', port)) == 0


def ensure_chrome_running() -> bool:
    """确保 Chrome 浏览器在调试模式下运行"""
    port = 9222
    chrome_path = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    user_data_dir = r"C:\selenium\automation_profile"

    if is_port_open(port):
        logger.info(f"✅ 端口 {port} 已开启，准备接管现有浏览器实例...")
    else:
        logger.info(f"🌐 端口 {port} 未开启，正在启动新浏览器实例...")
        command = [
            chrome_path,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check"
        ]
        try:
            subprocess.Popen(command)
            time.sleep(2)
        except Exception as e:
            logger.info(f"❌ 启动失败：{e}")
            return False
    return True


def clean_scraping_text(text: str) -> str:
    """清洗网页抓取的文本内容"""
    text = text.replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n\s*\n+', '\n', text)
    lines = [line.strip() for line in text.split('\n')]
    cleaned_text = '\n'.join([line for line in lines if line])
    return cleaned_text


def fetch_article_from_url(url: str) -> str:
    """使用 Playwright 抓取网页文章"""
    if not ensure_chrome_running():
        raise Exception("Chrome 浏览器环境未能成功启动或检测到端口")

    try:
        with sync_playwright() as p:
            logger.info("正在尝试接管已打开的 Chrome 实例 (port 9222)...")
            browser = p.chromium.connect_over_cdp("http://localhost:9222")
            
            try:
                context = browser.contexts[0]
                page = context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                time.sleep(10)
                
                full_text = page.evaluate("() => document.body.innerText")
                final_text = clean_scraping_text(full_text)
                
                if len(final_text) < 50:
                    raise Exception("抓取到的网页有效内容太少")
                
                return final_text
            finally:
                page.close()
                browser.close()
    except Exception as e:
        logger.info(f"Playwright 抓取失败：{e}")
        raise


# --- MongoDB 操作函数 ---

def get_collection():
    """获取 MongoDB collection 对象"""
    return Database().get_collection()


def get_chunks_collection():
    """获取文章切片 collection 对象"""
    db = Database()
    if db._chunks_collection is None:
        db.connect()
    return db._chunks_collection


def save_exam_session(session_id: str, data: dict):
    """保存或更新考核会话"""
    col = get_collection()
    data["session_id"] = session_id
    try:
        col.replace_one({"_id": session_id}, data, upsert=True)
        logger.info(f"Session {session_id} saved successfully.")
    except Exception as e:
        logger.error(f"Failed to save session {session_id}: {e}")
        raise


def load_exam_session(session_id: str) -> Optional[dict]:
    """读取特定的考核会话"""
    col = get_collection()
    try:
        doc = col.find_one({"_id": session_id})
        if doc:
            doc.pop("_id", None)
            return doc
    except Exception as e:
        logger.error(f"Failed to load session {session_id}: {e}")
        return None
    return None


def list_all_sessions(limit: int = 50) -> List[dict]:
    """获取所有会话列表"""
    col = get_collection()
    try:
        cursor = col.find({}, {"_id": 0}).sort("created_at", -1).limit(limit)
        return list(cursor)
    except Exception as e:
        logger.error(f"Failed to list sessions: {e}")
        return []


def delete_exam_session(session_id: str) -> bool:
    """删除会话记录"""
    col = get_collection()
    try:
        result = col.delete_one({"_id": session_id})
        success = result.deleted_count > 0
        if success:
            logger.info(f"Session {session_id} deleted successfully.")
        else:
            logger.warning(f"Session {session_id} not found for deletion.")
        return success
    except Exception as e:
        logger.error(f"Failed to delete session {session_id}: {e}")
        return False


def update_session_status(session_id: str, status: str, extra_data: dict = None):
    """原子更新会话状态及数据"""
    col = get_collection()
    update_fields = {"status": status, "updated_at": datetime.now().isoformat()}
    if extra_data:
        update_fields.update(extra_data)
    try:
        col.update_one({"_id": session_id}, {"$set": update_fields})
        logger.info(f"Session {session_id} status updated to {status}.")
    except Exception as e:
        logger.error(f"Failed to update session {session_id} status: {e}")
        raise


# --- 向量数据库操作 ---

def save_article_chunks(session_id: str, chunks: list):
    """
    保存文章切片到向量数据库
    
    Args:
        session_id: 会话 ID
        chunks: 切片列表，每个切片包含：
                - chunk_index: 切片索引
                - text: 切片文本
                - embedding: 向量表示 (可选)
                - start_pos: 在原文中的起始位置
                - end_pos: 在原文中的结束位置
    """
    col = get_chunks_collection()
    try:
        # 先删除该 session 的旧数据
        col.delete_many({"session_id": session_id})
        
        # 批量插入新数据
        for chunk in chunks:
            chunk["session_id"] = session_id
            chunk["created_at"] = datetime.now()
        
        if chunks:
            col.insert_many(chunks)
            logger.info(f"已保存 {len(chunks)} 个文章切片到 session {session_id}")
    except Exception as e:
        logger.error(f"Failed to save article chunks: {e}")
        raise


def search_similar_chunks(
    session_id: str, 
    query_embedding: list, 
    top_k: int = 3
) -> dict:
    """
    搜索与查询向量最相似的文章切片（使用余弦相似度）
    
    Args:
        session_id: 会话 ID
        query_embedding: 查询文本的向量表示
        top_k: 返回最相似的 K 个结果（供 LLM 评分使用）
    
    Returns:
        {
            "context_for_llm": [...],  # Top-K 分块，供 LLM 评分使用
            "evidence_for_display": [...]  # 阈值分级后的分块，供前端展示
        }
    """
    col = get_chunks_collection()
    
    try:
        # 从数据库读取所有该 session 的切片
        all_chunks = list(col.find({"session_id": session_id}, {"_id": 0}))
        
        if not all_chunks:
            return {"context_for_llm": [], "evidence_for_display": []}
        
        # 计算余弦相似度
        def cosine_similarity(vec1, vec2):
            dot_product = sum(a * b for a, b in zip(vec1, vec2))
            norm1 = sum(a * a for a in vec1) ** 0.5
            norm2 = sum(b * b for b in vec2) ** 0.5
            return dot_product / (norm1 * norm2) if norm1 > 0 and norm2 > 0 else 0
        
        # 为每个切片计算相似度
        scored_chunks = []
        for chunk in all_chunks:
            if "embedding" in chunk:
                score = cosine_similarity(query_embedding, chunk["embedding"])
                # 将原始分数转为百分比
                similarity_pct = score * 100
                scored_chunks.append({
                    **chunk,
                    "similarity_score": score,
                    "similarity_pct": similarity_pct
                })
        
        # 按相似度降序排序
        scored_chunks.sort(key=lambda x: x["similarity_pct"], reverse=True)
        
        # --- 阈值分级处理 ---
        evidence_for_display = []  # 最终展示给用户看的原文依据
        context_for_llm = scored_chunks[:top_k]  # 无论如何，给 LLM 评分的始终是 Top-K，保证评分效度
        
        for i, chunk in enumerate(scored_chunks):
            score = chunk["similarity_pct"]
            
            if score > 75:
                # 极强相关：全部放入
                evidence_for_display.append(chunk)
            elif 65 <= score <= 75:
                # 强相关：全部放入
                evidence_for_display.append(chunk)
            elif 50 <= score < 65:
                # 弱相关：只取前三个
                if len(evidence_for_display) < 3:
                    evidence_for_display.append(chunk)
            else:
                # 低于 50%：视为噪声，不展示给用户
                pass
        
        logger.info(f"   📊 检索到 {len(scored_chunks)} 个分块，Top-{top_k} 用于 LLM，{len(evidence_for_display)} 个用于展示")
        
        return {
            "context_for_llm": context_for_llm,
            "evidence_for_display": evidence_for_display
        }
        
    except Exception as e:
        logger.warning(f"向量搜索失败：{e}")
        return {"context_for_llm": [], "evidence_for_display": []}


def get_chunks_by_session(session_id: str) -> list:
    """获取指定会话的所有文章切片（包含向量数据）"""
    col = get_chunks_collection()
    try:
        # 不排除 embedding，确保返回完整数据（用于向量检索等场景）
        chunks = list(col.find({"session_id": session_id}, {"_id": 0}))
        return sorted(chunks, key=lambda x: x.get("chunk_index", 0))
    except Exception as e:
        logger.error(f"Failed to get chunks for session {session_id}: {e}")
        return []


def delete_session_chunks(session_id: str):
    """删除指定会话的所有文章切片"""
    col = get_chunks_collection()
    try:
        result = col.delete_many({"session_id": session_id})
        logger.info(f"已删除 session {session_id} 的 {result.deleted_count} 个切片")
    except Exception as e:
        logger.error(f"Failed to delete chunks for session {session_id}: {e}")


def search_chunks_for_question_generation(
    session_id: str,
    query_embeddings_map: dict,
    top_k_per_level: int = 5,
    top_k_per_query: int = 3
) -> dict:
    """
    【出题专用】基于多个查询向量批量检索并去重，返回每个层级最相关的分块
    
    Args:
        session_id: 会话 ID
        query_embeddings_map: 查询向量字典，格式：
                             {
                                 "basic": [embedding1, embedding2, ...],
                                 "apply": [embedding3, embedding4, ...],
                                 "advanced": [embedding5, embedding6, ...]
                             }
        top_k_per_level: 每个层级最终保留的分块数量（去重后）
        top_k_per_query: 每个查询初始检索的分块数量
    
    Returns:
        {
            "basic": [chunk1, chunk2, ...],
            "apply": [chunk3, chunk4, ...],
            "advanced": [chunk5, chunk6, ...]
        }
    """
    col = get_chunks_collection()
    results = {}
    
    try:
        # 读取所有该 session 的切片（带向量）
        all_chunks = list(col.find({"session_id": session_id}, {"_id": 0}))
        
        if not all_chunks:
            return {"basic": [], "apply": [], "advanced": []}
        
        # 余弦相似度计算函数
        def cosine_similarity(vec1, vec2):
            dot_product = sum(a * b for a, b in zip(vec1, vec2))
            norm1 = sum(a * a for a in vec1) ** 0.5
            norm2 = sum(b * b for b in vec2) ** 0.5
            return dot_product / (norm1 * norm2) if norm1 > 0 and norm2 > 0 else 0
        
        # 为每个层级检索和去重
        for level, embeddings in query_embeddings_map.items():
            level_chunks = []
            
            # 对该层级的每个查询进行检索
            for query_emb in embeddings:
                # 计算所有分块与当前查询的相似度
                scored_chunks = []
                for chunk in all_chunks:
                    if "embedding" in chunk:
                        score = cosine_similarity(query_emb, chunk["embedding"])
                        scored_chunks.append({
                            **chunk,
                            "similarity_score": score
                        })
                
                # 按相似度排序，取 Top-K
                scored_chunks.sort(key=lambda x: x["similarity_score"], reverse=True)
                level_chunks.extend(scored_chunks[:top_k_per_query])
            
            # 去重（按 chunk_index）
            seen_indices = set()
            unique_chunks = []
            for chunk in level_chunks:
                if chunk['chunk_index'] not in seen_indices:
                    seen_indices.add(chunk['chunk_index'])
                    unique_chunks.append(chunk)
            
            # 再次按相似度排序，取最终的 Top-K
            unique_chunks.sort(key=lambda x: x.get('similarity_score', 0), reverse=True)
            results[level] = unique_chunks[:top_k_per_level]
        
        # 确保三个层级都有返回值（即使为空）
        for level in ["basic", "apply", "advanced"]:
            if level not in results:
                results[level] = []
        
        return results
        
    except Exception as e:
        logger.warning(f"出题检索失败：{e}")
        return {"basic": [], "apply": [], "advanced": []}
