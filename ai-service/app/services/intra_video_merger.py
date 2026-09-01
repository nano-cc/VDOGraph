"""
视频内预合并（两跳消歧第一跳，docs/kg-phase4-commit-optimization.md §3）
纯本地计算：零 LLM、零图访问，可在锁外/任何时机执行。

对齐 Graphiti dedup_helpers.py 的 L1/L2，中文适配：
- L1：小写 + 压缩空白，哈希分组
- L2：3-gram shingle Jaccard ≥ 0.9 + Shannon 熵门控 ≥ 1.5（保留中文字符）
     + 包含关系补充（短名是长名子串）
- 并查集传递闭包（A≈B, B≈C → 同组）
- 边界对（单字语义翻转风险：养牛/养猪、CAT/RAT MEAT）不本地合并，输出给第二跳 LLM

设计原则：L1/L2 判"高置信相同"，"相似但拿不准"的一律留给 LLM。宁可错过（软错误）不可错并（硬错误）。
"""
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from app.core.logging import logger

# ---- 阈值（对齐 Graphiti dedup_helpers.py:31-36） ----
_NAME_ENTROPY_THRESHOLD = 1.5
_MIN_NAME_LENGTH = 6
_MIN_TOKEN_COUNT = 2
_FUZZY_JACCARD_THRESHOLD = 0.9
# Jaccard 在此区间内的对子不本地合并，作为边界对交给第二跳 LLM
_BORDERLINE_JACCARD_FLOOR = 0.5


class UnionFind:
    """并查集（路径压缩），对齐 Graphiti compressed_map 的传递闭包语义"""

    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


@dataclass
class VideoEntityGroup:
    """视频级实体组：第一跳产出，下游（对图匹配/LLM/写图）全程作为一个整体"""
    name: str                       # 组内最完整名字
    type: str                       # 组内类型众数
    descriptions: List[str]         # 全部描述（去重）
    surface_forms: List[str]        # 全部名字变体（后续进 aliases）
    sources: List[int]              # 来源片段编号
    mention_count: int              # 组内成员数（证据强度）
    member_keys: List[str] = field(default_factory=list)  # 组内成员的规范化名（关系映射用）


@dataclass
class VideoRelation:
    """视频级关系（端点已映射到实体组）"""
    source: str                     # 组代表名
    target: str
    descriptions: List[str]
    strength: int
    source_count: int
    sources: List[int]


# ==================== 规范化（对齐 Graphiti，中文适配） ====================

def normalize_exact(name: str) -> str:
    """L1：小写 + 压缩空白（对齐 _normalize_string_exact）"""
    return re.sub(r'\s+', ' ', (name or '').lower()).strip()


def normalize_fuzzy(name: str) -> str:
    """L2：保留字母数字和中文（Graphiti 原版 [^a-z0-9' ] 不含中文，必须适配）"""
    normalized = re.sub(r"[^a-z0-9'一-鿿 ]", ' ', normalize_exact(name))
    return re.sub(r'\s+', ' ', normalized).strip()


def name_entropy(normalized_name: str) -> float:
    """Shannon 熵（对齐 _name_entropy）：低熵名（短/重复字多）不信模糊匹配，交 LLM"""
    cleaned = normalized_name.replace(' ', '')
    if not cleaned:
        return 0.0
    counts: Dict[str, int] = defaultdict(int)
    for ch in cleaned:
        counts[ch] += 1
    total = len(cleaned)
    return -sum((c / total) * math.log2(c / total) for c in counts.values())


def has_high_entropy(normalized_name: str) -> bool:
    """熵门控（对齐 _has_high_entropy）"""
    if len(normalized_name) < _MIN_NAME_LENGTH and len(normalized_name.split()) < _MIN_TOKEN_COUNT:
        return False
    return name_entropy(normalized_name) >= _NAME_ENTROPY_THRESHOLD


def shingles(normalized_name: str) -> set:
    """3-gram 滑窗集合（对齐 _shingles，中文字符参与切窗）"""
    cleaned = normalized_name.replace(' ', '')
    if len(cleaned) < 2:
        return {cleaned} if cleaned else set()
    return {cleaned[i:i + 3] for i in range(len(cleaned) - 2)}


def jaccard(a: set, b: set) -> float:
    """Jaccard 相似度（对齐 _jaccard_similarity）"""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ==================== 实体合并 ====================

def merge_entities(raw_entities: List[Dict]) -> Tuple[List[VideoEntityGroup], List[Tuple[str, str]]]:
    """
    视频内实体预合并。
    返回：(视频级实体组列表, 边界对列表)
    边界对 = 相似但有单字语义翻转风险的对子（养牛/养猪类），留给第二跳 LLM 带上下文判。
    """
    if not raw_entities:
        return [], []

    # ---- L1：精确规范化分组 ----
    l1_groups: Dict[str, List[Dict]] = defaultdict(list)
    for e in raw_entities:
        l1_groups[normalize_exact(e['name'])].append(e)
    groups = list(l1_groups.values())
    rep_names = [max((m['name'] for m in g), key=len) for g in groups]
    fuzzy_names = [normalize_fuzzy(n) for n in rep_names]
    shingle_sets = [shingles(n) for n in fuzzy_names]

    # ---- L2：组间模糊配对 + 并查集闭包 ----
    uf = UnionFind(len(groups))
    borderline: List[Tuple[str, str]] = []
    for i in range(len(groups)):
        if not has_high_entropy(fuzzy_names[i]):
            continue
        for j in range(i + 1, len(groups)):
            if uf.find(i) == uf.find(j):
                continue
            if not has_high_entropy(fuzzy_names[j]):
                continue
            fa, fb = fuzzy_names[i].replace(' ', ''), fuzzy_names[j].replace(' ', '')
            # 包含关系补充（Graphiti 会漏的前缀扩展：阿尔萨斯洛林 → 阿尔萨斯洛林地区）
            if fa and fb and (fa in fb or fb in fa):
                uf.union(i, j)
                continue
            score = jaccard(shingle_sets[i], shingle_sets[j])
            if score >= _FUZZY_JACCARD_THRESHOLD:
                uf.union(i, j)
            elif score >= _BORDERLINE_JACCARD_FLOOR:
                borderline.append((rep_names[i], rep_names[j]))

    # ---- 闭包后的最终组聚合 ----
    merged: Dict[int, List[Dict]] = defaultdict(list)
    for i, g in enumerate(groups):
        merged[uf.find(i)].extend(g)

    result = []
    for members in merged.values():
        names = [m['name'] for m in members]
        types = [m.get('type', 'Other') for m in members]
        descriptions = []
        seen_desc = set()
        for m in members:
            d = m.get('description', '')
            if d and d not in seen_desc:
                seen_desc.add(d)
                descriptions.append(d)
        result.append(VideoEntityGroup(
            name=max(names, key=len),
            type=max(set(types), key=types.count),
            descriptions=descriptions,
            surface_forms=sorted(set(names)),
            sources=sorted(set(m['segment_index'] for m in members)),
            mention_count=len(members),
            member_keys=sorted(set(normalize_exact(n) for n in names)),
        ))

    logger.info(f"[INTRA_MERGE] 实体 {len(raw_entities)} → L1 {len(l1_groups)} 组 → 闭包后 {len(result)} 组"
                f"（边界对 {len(borderline)}）")
    return result, borderline


# ==================== 关系预合并 ====================

_REL_DESC_SIM_THRESHOLD = 0.8  # 同实体对内描述相似度合并阈值（与 conflict_detector 一致）


def merge_relationships(raw_relations: List[Dict], entity_groups: List[VideoEntityGroup]
                        ) -> Tuple[List[VideoRelation], int]:
    """
    关系端点映射到实体组（含全部 surface_forms），同组对预合并。
    返回：(视频级关系列表, 端点未映射丢弃数)
    """
    # 名字变体 → 组代表名
    name2group: Dict[str, str] = {}
    for g in entity_groups:
        for sf in g.surface_forms:
            name2group[normalize_exact(sf)] = g.name

    rel_groups: Dict[Tuple[str, str], List[Dict]] = defaultdict(list)
    dropped = 0
    for rel in raw_relations:
        src = name2group.get(normalize_exact(rel.get('source', '')))
        tgt = name2group.get(normalize_exact(rel.get('target', '')))
        if not src or not tgt:
            dropped += 1
            continue
        rel_groups[(src, tgt)].append(rel)

    result = []
    for (src, tgt), rels in rel_groups.items():
        # 同实体对内按描述相似度聚类合并（简单贪心：相似度≥0.8 归一簇）
        clusters: List[List[Dict]] = []
        for rel in rels:
            placed = False
            for cluster in clusters:
                pivot = cluster[0]['description']
                if _desc_similar(pivot, rel.get('description', '')) >= _REL_DESC_SIM_THRESHOLD:
                    cluster.append(rel)
                    placed = True
                    break
            if not placed:
                clusters.append([rel])

        for cluster in clusters:
            result.append(VideoRelation(
                source=src,
                target=tgt,
                descriptions=[r.get('description', '') for r in cluster],
                strength=max(r.get('strength', 5) for r in cluster),
                source_count=len(cluster),
                sources=sorted(set(r['segment_index'] for r in cluster)),
            ))

    logger.info(f"[INTRA_MERGE] 关系 {len(raw_relations)} → {len(result)}（端点未映射丢弃 {dropped}）")
    return result, dropped


def _desc_similar(a: str, b: str) -> float:
    """描述相似度：3-gram Jaccard（与 L2 同名判定同度量，本地零成本）"""
    return jaccard(shingles(normalize_fuzzy(a)), shingles(normalize_fuzzy(b)))


# ==================== 入口 ====================

def merge_video_extractions(raw_entities: List[Dict], raw_relations: List[Dict]
                            ) -> Tuple[List[VideoEntityGroup], List[VideoRelation], List[Tuple[str, str]]]:
    """第一跳入口：实体预合并 + 关系预合并。返回（实体组, 视频级关系, 边界对）"""
    entity_groups, borderline = merge_entities(raw_entities)
    relations, _dropped = merge_relationships(raw_relations, entity_groups)
    return entity_groups, relations, borderline
