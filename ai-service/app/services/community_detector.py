"""
社区检测服务（Leiden 层次化社区划分，参考 GraphRAG）
"""
from typing import List, Dict, Optional
import networkx as nx
from app.models.community import Community, CommunityEntity, CommunityRelationship
from app.clients.deepseek import DeepSeekClient
from app.core.logging import logger

try:
    import igraph as ig
    import leidenalg
    LEIDEN_AVAILABLE = True
except ImportError:
    LEIDEN_AVAILABLE = False
    logger.warning("igraph/leidenalg not installed, fallback to connected components")


# 层次化参数（参考 GraphRAG：max_cluster_size=10）
MAX_CLUSTER_SIZE = 10   # 社区实体数超过此值则递归细分
MAX_LEVEL = 3           # 最大层级


class CommunityDetector:
    def __init__(self):
        self.deepseek_client = DeepSeekClient()
        self._id_counter = 0

    async def detect(self, entities: List[dict], relationships: List[dict]) -> Dict:
        """
        社区检测（Leiden 层次化划分，igraph 不可用时降级为连通分量）
        """
        self._id_counter = 0

        if LEIDEN_AVAILABLE:
            communities = self._hierarchical_leiden(entities, relationships)
        else:
            G = nx.Graph()
            for entity in entities:
                G.add_node(entity['id'], name=entity['name'], type=entity['type'])
            for rel in relationships:
                G.add_edge(rel['source_entity_id'], rel['target_entity_id'],
                          strength=rel['strength'], description=rel['description'])
            connected_components = list(nx.connected_components(G))
            logger.info(f"Found {len(connected_components)} connected components")
            communities = self._build_communities(connected_components, entities, relationships)

        # 生成社区摘要
        communities = await self._generate_summaries(communities)

        level_counts = {}
        for c in communities:
            level_counts[c.level] = level_counts.get(c.level, 0) + 1

        return {
            'communities': communities,
            'statistics': {
                'total_communities': len(communities),
                'total_entities': len(entities),
                'total_relationships': len(relationships),
                'avg_community_size': sum(c.entity_count for c in communities) / len(communities) if communities else 0,
                'level_counts': level_counts,
                'algorithm': 'leiden_hierarchical' if LEIDEN_AVAILABLE else 'connected_components'
            }
        }

    def _next_id(self) -> str:
        cid = f"community_{self._id_counter}"
        self._id_counter += 1
        return cid

    def _hierarchical_leiden(self, entities: List[dict], relationships: List[dict]) -> List[Community]:
        """
        Leiden 层次化划分（GraphRAG 式）：
        对整图跑 Leiden，社区规模超过 MAX_CLUSTER_SIZE 的对子图递归细分，形成 level 0/1/2 层级
        """
        entity_map = {e['id']: e for e in entities}
        communities: List[Community] = []

        def leiden_partition(entity_ids: List[str], resolution: float = 1.0) -> List[List[str]]:
            """对给定实体子集跑 Leiden，返回分组（实体 id 列表的列表）"""
            G = ig.Graph()
            G.add_vertices(entity_ids)
            entity_id_set = set(entity_ids)
            edges = []
            weights = []
            for rel in relationships:
                src, tgt = rel['source_entity_id'], rel['target_entity_id']
                if src in entity_id_set and tgt in entity_id_set:
                    edges.append((src, tgt))
                    weights.append(max(rel.get('strength', 1), 1))
            if edges:
                G.add_edges(edges)
                partition = leidenalg.find_partition(
                    G, leidenalg.RBConfigurationVertexPartition,
                    weights=weights, resolution_parameter=resolution
                )
            else:
                partition = leidenalg.find_partition(
                    G, leidenalg.RBConfigurationVertexPartition,
                    resolution_parameter=resolution
                )
            return [[G.vs[i]['name'] for i in group] for group in partition]

        def recurse(entity_ids: List[str], level: int, parent_id: Optional[str]):
            groups = leiden_partition(entity_ids)
            for group in groups:
                community_id = self._next_id()
                community = self._build_community(group, entity_map, relationships, community_id, level, parent_id)
                communities.append(community)

                # 规模超阈值且未到最大层级：递归细分
                if len(group) > MAX_CLUSTER_SIZE and level < MAX_LEVEL - 1:
                    recurse(group, level + 1, community_id)

        recurse([e['id'] for e in entities], 0, None)

        logger.info(f"[COMMUNITY] Leiden hierarchical: {len(communities)} communities, levels: {dict((l, sum(1 for c in communities if c.level == l)) for l in set(c.level for c in communities))}")
        return communities

    def _build_community(self, entity_ids: List[str], entity_map: Dict, relationships: List[dict],
                         community_id: str, level: int, parent_id: Optional[str]) -> Community:
        """构建单个社区"""
        entity_list = [entity_map[eid] for eid in entity_ids if eid in entity_map]
        entity_id_set = set(entity_ids)
        community_relationships = [
            rel for rel in relationships
            if rel['source_entity_id'] in entity_id_set and rel['target_entity_id'] in entity_id_set
        ]

        all_sources = set()
        for entity in entity_list:
            for source in entity.get('sources', []):
                segment_id = source.get('segment_id')
                if segment_id:
                    all_sources.add(segment_id)

        return Community(
            id=community_id,
            level=level,
            parent_id=parent_id,
            entity_count=len(entity_list),
            relationship_count=len(community_relationships),
            entities=[
                CommunityEntity(
                    id=e['id'],
                    name=e['name'],
                    type=e['type'],
                    description=e.get('description', ''),
                    source_count=e.get('source_count', 1)
                )
                for e in entity_list
            ],
            relationships=[
                CommunityRelationship(
                    source=rel['source_entity_name'],
                    target=rel['target_entity_name'],
                    description=rel['description'],
                    strength=rel['strength']
                )
                for rel in community_relationships
            ],
            source_segments=sorted(list(all_sources)),
            summary=None
        )

    def _build_communities(self, connected_components: List[set], entities: List[dict], relationships: List[dict]) -> List[Community]:
        """构建社区（连通分量兜底路径）"""
        entity_map = {e['id']: e for e in entities}

        communities = []
        for component in connected_components:
            community = self._build_community(
                list(component), entity_map, relationships,
                self._next_id(), 0, None
            )
            communities.append(community)

        # 按实体数量排序
        communities.sort(key=lambda x: x.entity_count, reverse=True)

        return communities

    async def _generate_summaries(self, communities: List[Community]) -> List[Community]:
        """生成社区摘要"""
        for community in communities:
            logger.info(f"Generating summary for community {community.id} ({community.entity_count} entities)")

            # 孤立节点：直接用实体描述
            if community.entity_count == 1 and community.relationship_count == 0:
                entity = community.entities[0]
                community.summary = f"视频提到「{entity.name}」：{entity.description}"
                continue

            # 多实体社区：用 LLM 生成
            prompt = self._build_summary_prompt(community)

            try:
                summary = await self.deepseek_client.generate_summary(prompt)
                community.summary = summary
            except Exception as e:
                logger.error(f"Failed to generate summary: {e}")
                community.summary = f"本社区包含 {community.entity_count} 个实体，{community.relationship_count} 个关系。"

        return communities

    def _build_summary_prompt(self, community: Community) -> str:
        """构建社区摘要 Prompt"""
        entity_names = [e.name for e in community.entities]
        relationship_descriptions = [f"{r.source} -> {r.target}: {r.description}" for r in community.relationships[:10]]

        return f"""请为以下社区生成一个简短的摘要（50-100字）。

社区包含的实体:
{', '.join(entity_names)}

社区包含的关系（前 10 个）:
{chr(10).join(relationship_descriptions)}

请生成一个摘要，说明这个社区讨论的主题是什么。

摘要:"""
