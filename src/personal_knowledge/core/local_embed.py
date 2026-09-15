"""本地 GPU embedding 模块(sentence-transformers + bge-small-zh)。

替代 ollama_embed.py。ollama 在批量 embedding 时不稳定(hang 死),
本模块用 sentence-transformers 直接在 GPU 上本地计算,优势:
- 速度:7723 条约 53 秒(ollama 需 1 小时+且 hang)
- 稳定:本地内存计算,无网络请求,绝不会 hang
- 隐私:数据完全不出机器
- 模型加载:0.6 秒(ollama 冷启动 58 秒)

模型: BAAI/bge-small-zh-v1.5(512 维,中文优,95MB)
  - 通过 PERSONAL_DATA_EMBED_MODEL_PATH 配置本机模型目录

接口与 ollama_embed 兼容(build_vector_store / search_vectors 可平滑切换):
- embed(text) -> list[float]
- embed_batch(texts) -> list[Optional[list[float]]]
- verify_model() -> (ok, msg, dim)
"""

from __future__ import annotations

import os
from typing import Optional

from personal_knowledge.core.runtime_config import embedding_model_path

# 禁用 HF 在线检查(纯本地模型,避免网络错误)
os.environ.setdefault('TRANSFORMERS_OFFLINE', '1')
os.environ.setdefault('HF_HUB_OFFLINE', '1')


# === 配置 ===
# 模型路径(从 C 盘 modelscope 缓存迁移到 D 盘,释放系统盘空间)
MODEL_PATH = os.environ.get("PERSONAL_DATA_EMBED_MODEL_PATH", "")
EMBED_MODEL = "bge-small-zh-v1.5"  # 模型名(展示用,兼容 ollama_embed 接口)
EMBED_DIM = 512  # bge-small-zh-v1.5 维度
DEVICE = os.environ.get("PERSONAL_DATA_EMBED_DEVICE", "cuda")
# 默认 GPU；MCP 等短生命周期子进程可设置为 cpu，避免与常驻 API
# 同时初始化 CUDA。模型和向量维度保持一致。

# 模型单例(避免重复加载)
_MODEL = None


def _get_model():
    """懒加载模型单例。首次调用加载,后续复用。"""
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    from sentence_transformers import SentenceTransformer
    model_path = str(embedding_model_path())
    # 尝试 GPU,失败回退 CPU
    try:
        _MODEL = SentenceTransformer(model_path, device=DEVICE)
    except Exception:
        _MODEL = SentenceTransformer(model_path, device="cpu")
    return _MODEL


def embed(text: str) -> list[float]:
    """单条文本向量化。返回 float 向量(512 维)。"""
    if not text or not text.strip():
        raise ValueError("空文本无法 embedding")
    model = _get_model()
    vec = model.encode([text], batch_size=1)[0]
    return vec.tolist()


def embed_batch(
    texts: list[str],
    batch_size: int = 64,
) -> list[Optional[list[float]]]:
    """批量向量化。返回与 texts 等长的列表,空文本为 None。

    本地 GPU 计算,sentence-transformers 自动批处理,无网络请求,不会 hang。
    """
    if not texts:
        return []
    model = _get_model()
    # 标记空文本位置
    results: list[Optional[list[float]]] = [None] * len(texts)
    indices_ok: list[int] = []
    clean_texts: list[str] = []
    for i, t in enumerate(texts):
        c = (t or "").strip()
        if c:
            indices_ok.append(i)
            clean_texts.append(c)
    if not clean_texts:
        return results
    # 批量 encode(GPU 加速)
    embs = model.encode(clean_texts, batch_size=batch_size, show_progress_bar=False)
    for idx, emb in zip(indices_ok, embs):
        results[idx] = emb.tolist()
    return results


# 兼容别名(build_vector_store 调用的名字)
embed_batch_native = embed_batch


def verify_model() -> tuple[bool, str, Optional[int]]:
    """验证模型可用性。返回 (可用, 说明, 维度)。

    首次调用会加载模型(约 0.6 秒)。
    """
    try:
        model = _get_model()
        # 测一条确认能出向量
        vec = model.encode(["验证"], batch_size=1)[0]
        import torch
        device = "GPU" if torch.cuda.is_available() else "CPU"
        return True, f"模型 bge-small-zh-v1.5 可用({device})", len(vec)
    except Exception as e:
        return False, f"模型加载失败: {e}", None


def is_model_available(model: str = "") -> bool:
    """兼容接口:检查模型是否可用(本地模型直接看路径)。"""
    try:
        return embedding_model_path().is_dir()
    except RuntimeError:
        return False
