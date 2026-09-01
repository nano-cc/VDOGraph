"""
Neo4j 客户端
负责 Neo4j 图数据库的所有读写操作
"""
from typing import List, Dict, Optional
from neo4j import GraphDatabase
from app.core.config import settings
from app.core.logging import logger


class Neo4jClient:
    def __init__(self):
        self.driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password)
        )

    def close(self):
        self.driver.close()

    def create_indexes(self):
        """创建所有索引"""
        with self.driver.session() as session:
            # 实体名称向量索引
            session.run("""
                CREATE VECTOR INDEX entity_name_embedding_index IF NOT EXISTS
                FOR (e:Entity)
                ON e.name_embedding
                OPTIONS {
                  indexConfig: {
                    `vector.dimensions`: 1024,
                    `vector.similarity_function`: 'cosine'
                  }
                }
            """)

            # 实体描述向量索引
            session.run("""
                CREATE VECTOR INDEX entity_description_embedding_index IF NOT EXISTS
                FOR (e:Entity)
                ON e.description_embedding
                OPTIONS {
                  indexConfig: {
                    `vector.dimensions`: 1024,
                    `vector.similarity_function`: 'cosine'
                  }
                }
            """)

            # 关系描述向量索引
            session.run("""
                CREATE VECTOR INDEX relationship_description_embedding_index IF NOT EXISTS
                FOR ()-[r:RELATES_TO]-()
                ON r.description_embedding
                OPTIONS {
                  indexConfig: {
                    `vector.dimensions`: 1024,
                    `vector.similarity_function`: 'cosine'
                  }
                }
            """)

            # 社区摘要向量索引
            session.run("""
                CREATE VECTOR INDEX community_summary_embedding_index IF NOT EXISTS
                FOR (c:Community)
                ON c.summary_embedding
                OPTIONS {
                  indexConfig: {
                    `vector.dimensions`: 1024,
                    `vector.similarity_function`: 'cosine'
                  }
                }
            """)

            # 片段 ASR 向量索引
            session.run("""
                CREATE VECTOR INDEX segment_transcript_embedding_index IF NOT EXISTS
                FOR (s:Segment)
                ON s.transcript_embedding
                OPTIONS {
                  indexConfig: {
                    `vector.dimensions`: 1024,
                    `vector.similarity_function`: 'cosine'
                  }
                }
            """)

            # 片段 OCR 向量索引
            session.run("""
                CREATE VECTOR INDEX segment_ocr_embedding_index IF NOT EXISTS
                FOR (s:Segment)
                ON s.ocr_embedding
                OPTIONS {
                  indexConfig: {
                    `vector.dimensions`: 1024,
                    `vector.similarity_function`: 'cosine'
                  }
                }
            """)

            # 视频标题向量索引
            session.run("""
                CREATE VECTOR INDEX media_title_embedding_index IF NOT EXISTS
                FOR (m:Media)
                ON m.title_embedding
                OPTIONS {
                  indexConfig: {
                    `vector.dimensions`: 1024,
                    `vector.similarity_function`: 'cosine'
                  }
                }
            """)

            # 普通索引
            session.run("CREATE INDEX entity_id_index IF NOT EXISTS FOR (e:Entity) ON (e.id)")
            session.run("CREATE INDEX entity_name_index IF NOT EXISTS FOR (e:Entity) ON (e.name)")
            session.run("CREATE INDEX segment_id_index IF NOT EXISTS FOR (s:Segment) ON (s.id)")
            session.run("CREATE INDEX community_id_index IF NOT EXISTS FOR (c:Community) ON (c.id)")
            session.run("CREATE INDEX media_id_index IF NOT EXISTS FOR (m:Media) ON (m.id)")
            # group_id 隔离（三期）：预过滤走 B-tree，向量相似度在过滤后的子集上精确计算
            session.run("CREATE INDEX entity_group_index IF NOT EXISTS FOR (e:Entity) ON (e.group_id)")
            session.run("CREATE INDEX segment_group_index IF NOT EXISTS FOR (s:Segment) ON (s.group_id)")
            session.run("CREATE INDEX community_group_index IF NOT EXISTS FOR (c:Community) ON (c.group_id)")
            session.run("CREATE INDEX media_group_index IF NOT EXISTS FOR (m:Media) ON (m.group_id)")

            # 全文索引（BM25 召回路，Graphiti 式混合检索）
            session.run("""
                CREATE FULLTEXT INDEX entity_fulltext IF NOT EXISTS
                FOR (e:Entity) ON EACH [e.name, e.description]
            """)
            session.run("""
                CREATE FULLTEXT INDEX relationship_fulltext IF NOT EXISTS
                FOR ()-[r:RELATES_TO]-() ON EACH [r.description]
            """)
            session.run("""
                CREATE FULLTEXT INDEX segment_fulltext IF NOT EXISTS
                FOR (s:Segment) ON EACH [s.transcript]
            """)
            session.run("""
                CREATE FULLTEXT INDEX community_fulltext IF NOT EXISTS
                FOR (c:Community) ON EACH [c.summary]
            """)

            logger.info("[NEO4J] All indexes created")

    def save_media(self, media: Dict):
        """保存视频节点（MERGE 幂等）"""
        with self.driver.session() as session:
            session.run("""
                MERGE (m:Media {id: $id})
                ON CREATE SET m.created_at = datetime()
                SET m.group_id = $group_id,
                    m.title = $title,
                    m.path = $path,
                    m.duration_ms = $duration_ms,
                    m.title_embedding = $title_embedding,
                    m.updated_at = datetime()
            """, media)

    def link_segment_to_media(self, segment_id: str, media_id: str):
        """链接片段到视频"""
        with self.driver.session() as session:
            session.run("""
                MATCH (s:Segment {id: $segment_id})
                MATCH (m:Media {id: $media_id})
                MERGE (m)-[:HAS_SEGMENT]->(s)
            """, {'segment_id': segment_id, 'media_id': media_id})

    def save_entity(self, entity: Dict):
        """保存实体（MERGE 幂等）"""
        with self.driver.session() as session:
            session.run("""
                MERGE (e:Entity {id: $id})
                ON CREATE SET e.created_at = datetime()
                SET e.group_id = $group_id,
                    e.name = $name,
                    e.type = $type,
                    e.description = $description,
                    e.name_embedding = $name_embedding,
                    e.description_embedding = $description_embedding,
                    e.aliases = $aliases,
                    e.source_count = $source_count,
                    e.updated_at = datetime()
            """, entity)

    def save_relationship(self, relationship: Dict):
        """保存关系（MERGE 幂等，按确定性 id）"""
        with self.driver.session() as session:
            session.run("""
                MATCH (source:Entity {id: $source_entity_id})
                MATCH (target:Entity {id: $target_entity_id})
                MERGE (source)-[r:RELATES_TO {id: $id}]->(target)
                ON CREATE SET r.created_at = datetime()
                SET r.group_id = $group_id,
                    r.description = $description,
                    r.strength = $strength,
                    r.confidence = $confidence,
                    r.description_embedding = $description_embedding,
                    r.source_count = $source_count,
                    r.source_segment_ids = $source_segment_ids
            """, relationship)

    def save_community(self, community: Dict):
        """保存社区（MERGE 幂等）"""
        with self.driver.session() as session:
            session.run("""
                MERGE (c:Community {id: $id})
                ON CREATE SET c.created_at = datetime()
                SET c.group_id = $group_id,
                    c.level = $level,
                    c.parent_id = $parent_id,
                    c.entity_count = $entity_count,
                    c.relationship_count = $relationship_count,
                    c.summary = $summary,
                    c.summary_embedding = $summary_embedding,
                    c.updated_at = datetime()
            """, community)

    def save_segment(self, segment: Dict):
        """保存片段（MERGE 幂等）"""
        with self.driver.session() as session:
            session.run("""
                MERGE (s:Segment {id: $id})
                ON CREATE SET s.created_at = datetime()
                SET s.group_id = $group_id,
                    s.media_id = $media_id,
                    s.segment_index = $segment_index,
                    s.start_ms = $start_ms,
                    s.end_ms = $end_ms,
                    s.transcript = $transcript,
                    s.ocr_texts = $ocr_texts,
                    s.frame_urls = $frame_urls,
                    s.transcript_embedding = $transcript_embedding,
                    s.ocr_embedding = $ocr_embedding,
                    s.updated_at = datetime()
            """, segment)

    def link_entity_to_segment(self, entity_id: str, segment_id: str, segment_index: int, confidence: float, source_type: str, surface_form: str = None):
        """链接实体到片段（追溯，MERGE 幂等；surface_form 记录该片段中实际出现的实体写法）"""
        with self.driver.session() as session:
            session.run("""
                MATCH (e:Entity {id: $entity_id})
                MATCH (s:Segment {id: $segment_id})
                MERGE (e)-[r:MENTIONED_IN]->(s)
                SET r.segment_index = $segment_index,
                    r.confidence = $confidence,
                    r.source_type = $source_type,
                    r.surface_form = $surface_form
            """, {
                'entity_id': entity_id,
                'segment_id': segment_id,
                'segment_index': segment_index,
                'confidence': confidence,
                'source_type': source_type,
                'surface_form': surface_form
            })

    def link_entity_to_community(self, entity_id: str, community_id: str):
        """链接实体到社区（MERGE 幂等）"""
        with self.driver.session() as session:
            session.run("""
                MATCH (e:Entity {id: $entity_id})
                MATCH (c:Community {id: $community_id})
                MERGE (e)-[:BELONGS_TO]->(c)
            """, {
                'entity_id': entity_id,
                'community_id': community_id
            })

    def link_community_to_segment(self, community_id: str, segment_id: str):
        """链接社区到片段（追溯，MERGE 幂等）"""
        with self.driver.session() as session:
            session.run("""
                MATCH (c:Community {id: $community_id})
                MATCH (s:Segment {id: $segment_id})
                MERGE (c)-[:CONTAINS]->(s)
            """, {
                'community_id': community_id,
                'segment_id': segment_id
            })

    def save_entities_batch(self, entities: List[Dict]):
        """批量保存实体（UNWIND 单语句）"""
        if not entities:
            return
        with self.driver.session() as session:
            session.run("""
                UNWIND $batch AS e
                MERGE (n:Entity {id: e.id})
                ON CREATE SET n.created_at = datetime()
                SET n.group_id = e.group_id,
                    n.name = e.name,
                    n.type = e.type,
                    n.description = e.description,
                    n.name_embedding = e.name_embedding,
                    n.description_embedding = e.description_embedding,
                    n.aliases = e.aliases,
                    n.source_count = e.source_count,
                    n.updated_at = datetime()
            """, {'batch': entities})

    def save_relationships_batch(self, relationships: List[Dict]):
        """批量保存关系（UNWIND 单语句）"""
        if not relationships:
            return
        with self.driver.session() as session:
            session.run("""
                UNWIND $batch AS rel
                MATCH (source:Entity {id: rel.source_entity_id})
                MATCH (target:Entity {id: rel.target_entity_id})
                MERGE (source)-[r:RELATES_TO {id: rel.id}]->(target)
                ON CREATE SET r.created_at = datetime()
                SET r.group_id = rel.group_id,
                    r.description = rel.description,
                    r.strength = rel.strength,
                    r.confidence = rel.confidence,
                    r.description_embedding = rel.description_embedding,
                    r.source_count = rel.source_count,
                    r.source_segment_ids = rel.source_segment_ids
            """, {'batch': relationships})

    def link_entity_to_segment_batch(self, links: List[Dict]):
        """批量链接实体到片段（UNWIND 单语句）"""
        if not links:
            return
        with self.driver.session() as session:
            session.run("""
                UNWIND $batch AS l
                MATCH (e:Entity {id: l.entity_id})
                MATCH (s:Segment {id: l.segment_id})
                MERGE (e)-[r:MENTIONED_IN]->(s)
                SET r.segment_index = l.segment_index,
                    r.confidence = l.confidence,
                    r.source_type = l.source_type,
                    r.surface_form = l.surface_form
            """, {'batch': links})

    def get_media_segments(self, media_id: int) -> List[Dict]:
        """获取视频的所有片段（含抽取结果，用于幂等跳过）"""
        with self.driver.session() as session:
            result = session.run("""
                MATCH (m:Media {id: $media_id})-[:HAS_SEGMENT]->(s:Segment)
                RETURN s
                ORDER BY s.segment_index
            """, {'media_id': f"media_{media_id}"})

            return [dict(record['s']) for record in result]

    def save_segment_extraction(self, segment_id: str, extraction_json: str):
        """保存片段的原始抽取结果（断点续跑用）"""
        with self.driver.session() as session:
            session.run("""
                MATCH (s:Segment {id: $segment_id})
                SET s.raw_extraction = $extraction_json,
                    s.extracted_at = datetime()
            """, {'segment_id': segment_id, 'extraction_json': extraction_json})

    def delete_media_communities(self, media_id: int):
        """删除视频的旧社区及边（重跑时先清理，避免重复挂载）"""
        with self.driver.session() as session:
            session.run("""
                MATCH (c:Community)
                WHERE c.id STARTS WITH $prefix
                DETACH DELETE c
            """, {'prefix': f"media_{media_id}_"})

    def find_entity_by_name(self, name: str, group_id: str) -> Optional[Dict]:
        """按名称查询实体（group 内）"""
        with self.driver.session() as session:
            result = session.run("""
                MATCH (e:Entity {name: $name, group_id: $gid})
                RETURN e
                LIMIT 1
            """, {'name': name, 'gid': group_id})

            record = result.single()
            return dict(record['e']) if record else None

    def get_group_entities(self, group_id: str) -> List[Dict]:
        """读取本 group 全部实体的轻量字段（对图 L1/L2 索引、增量复查用，#68）"""
        with self.driver.session() as session:
            result = session.run("""
                MATCH (e:Entity {group_id: $gid})
                RETURN e.id AS id, e.name AS name, e.aliases AS aliases,
                       e.type AS type, e.description AS description,
                       e.created_at AS created_at
            """, {'gid': group_id})
            return [dict(r) for r in result]

    def get_entities_created_after(self, group_id: str, epoch_seconds: float) -> List[Dict]:
        """增量复查用（#68）：某时间点之后新建的实体（语义幻影检测）"""
        with self.driver.session() as session:
            result = session.run("""
                MATCH (e:Entity {group_id: $gid})
                WHERE e.created_at IS NOT NULL AND e.created_at > datetime({epochSeconds: $ts})
                RETURN e.id AS id, e.name AS name, e.aliases AS aliases,
                       e.type AS type, e.description AS description,
                       e.name_embedding AS name_embedding
            """, {'gid': group_id, 'ts': int(epoch_seconds)})
            return [dict(r) for r in result]

    def find_similar_entities(self, embedding: List[float], group_id: str, top_k: int = 5, threshold: float = 0.5,
                              media_id: int = None) -> List[Dict]:
        """向量相似度查询（group 内闭包：B-tree 预过滤 + 精确余弦，召回无损）
        media_id 可选：限定在该视频片段中被提及的实体（单视频问答）"""
        with self.driver.session() as session:
            result = session.run("""
                MATCH (node:Entity {group_id: $gid})
                WHERE node.name_embedding IS NOT NULL
                  AND ($mid IS NULL OR EXISTS { (node)-[:MENTIONED_IN]->(:Segment {media_id: $mid}) })
                WITH node, vector.similarity.cosine(node.name_embedding, $embedding) AS score
                WHERE score >= $threshold
                RETURN node, score
                ORDER BY score DESC
                LIMIT $top_k
            """, {
                'gid': group_id,
                'mid': media_id,
                'top_k': top_k,
                'embedding': embedding,
                'threshold': threshold
            })

            return [
                {
                    'entity': dict(record['node']),
                    'score': record['score']
                }
                for record in result
            ]

    def search_entities_by_description(self, embedding: List[float], group_id: str, top_k: int = 5, threshold: float = 0.5,
                                       media_id: int = None) -> List[Dict]:
        """按描述向量检索实体（group 内闭包，media_id 可选限定单视频）"""
        with self.driver.session() as session:
            result = session.run("""
                MATCH (node:Entity {group_id: $gid})
                WHERE node.description_embedding IS NOT NULL
                  AND ($mid IS NULL OR EXISTS { (node)-[:MENTIONED_IN]->(:Segment {media_id: $mid}) })
                WITH node, vector.similarity.cosine(node.description_embedding, $embedding) AS score
                WHERE score >= $threshold
                RETURN node, score
                ORDER BY score DESC
                LIMIT $top_k
            """, {'gid': group_id, 'mid': media_id, 'top_k': top_k, 'embedding': embedding, 'threshold': threshold})

            return [{'entity': dict(record['node']), 'score': record['score']} for record in result]

    def search_segments(self, embedding: List[float], group_id: str, top_k: int = 5, threshold: float = 0.5,
                        media_id: int = None) -> List[Dict]:
        """按 ASR 文本向量检索片段（group 内闭包，media_id 可选限定单视频）"""
        with self.driver.session() as session:
            result = session.run("""
                MATCH (node:Segment {group_id: $gid})
                WHERE node.transcript_embedding IS NOT NULL
                  AND ($mid IS NULL OR node.media_id = $mid)
                WITH node, vector.similarity.cosine(node.transcript_embedding, $embedding) AS score
                WHERE score >= $threshold
                RETURN node, score
                ORDER BY score DESC
                LIMIT $top_k
            """, {'gid': group_id, 'mid': media_id, 'top_k': top_k, 'embedding': embedding, 'threshold': threshold})

            return [{'segment': dict(record['node']), 'score': record['score']} for record in result]

    def search_communities(self, embedding: List[float], group_id: str, top_k: int = 5, threshold: float = 0.3,
                           media_id: int = None) -> List[Dict]:
        """按摘要向量检索社区（group 内闭包；阈值较低：摘要与问题的语义距离天然更远）
        media_id 可选：限定覆盖了该视频片段的社区（单视频问答）"""
        with self.driver.session() as session:
            result = session.run("""
                MATCH (node:Community {group_id: $gid})
                WHERE node.summary_embedding IS NOT NULL
                  AND ($mid IS NULL OR EXISTS { (node)-[:CONTAINS]->(:Segment {media_id: $mid}) })
                WITH node, vector.similarity.cosine(node.summary_embedding, $embedding) AS score
                WHERE score >= $threshold
                RETURN node, score
                ORDER BY score DESC
                LIMIT $top_k
            """, {'gid': group_id, 'mid': media_id, 'top_k': top_k, 'embedding': embedding, 'threshold': threshold})

            return [{'community': dict(record['node']), 'score': record['score']} for record in result]

    @staticmethod
    def _escape_lucene(query: str) -> str:
        """转义 Lucene 查询特殊字符"""
        special = '+-=&|><!(){}[]^"~*?:\\/'
        return ''.join('\\' + c if c in special else c for c in query)

    def search_entities_fulltext(self, query: str, group_id: str, top_k: int = 10, media_id: int = None) -> List[Dict]:
        """实体全文检索（BM25，group 内闭包，media_id 可选限定单视频）"""
        with self.driver.session() as session:
            result = session.run("""
                CALL db.index.fulltext.queryNodes('entity_fulltext', $query)
                YIELD node, score
                WHERE node.group_id = $gid
                  AND ($mid IS NULL OR EXISTS { (node)-[:MENTIONED_IN]->(:Segment {media_id: $mid}) })
                RETURN node, score
                ORDER BY score DESC
                LIMIT $top_k
            """, {'query': self._escape_lucene(query), 'gid': group_id, 'mid': media_id, 'top_k': top_k})
            return [{'entity': dict(r['node']), 'score': r['score']} for r in result]

    def search_relationships_fulltext(self, query: str, group_id: str, top_k: int = 10, media_id: int = None) -> List[Dict]:
        """关系全文检索（BM25，group 内闭包，media_id 可选限定单视频来源）"""
        with self.driver.session() as session:
            result = session.run("""
                CALL db.index.fulltext.queryRelationships('relationship_fulltext', $query)
                YIELD relationship, score
                WHERE relationship.group_id = $gid
                  AND ($mid IS NULL OR ANY(sid IN relationship.source_segment_ids WHERE sid STARTS WITH $mprefix))
                MATCH (s)-[relationship]->(t)
                RETURN s.id as source_id, s.name as source_name,
                       t.id as target_id, t.name as target_name,
                       relationship.description as description,
                       relationship.strength as strength,
                       relationship.source_segment_ids as source_segment_ids,
                       score
                ORDER BY score DESC
                LIMIT $top_k
            """, {'query': self._escape_lucene(query), 'gid': group_id, 'mid': media_id,
                  'mprefix': f"media_{media_id}_", 'top_k': top_k})
            return [dict(r) for r in result]

    def search_relationships_by_vector(self, embedding: List[float], group_id: str, top_k: int = 10, threshold: float = 0.5,
                                       media_id: int = None) -> List[Dict]:
        """关系向量检索（group 内闭包，精确余弦，media_id 可选限定单视频来源）"""
        with self.driver.session() as session:
            result = session.run("""
                MATCH (s)-[relationship:RELATES_TO]->(t)
                WHERE relationship.group_id = $gid AND relationship.description_embedding IS NOT NULL
                  AND ($mid IS NULL OR ANY(sid IN relationship.source_segment_ids WHERE sid STARTS WITH $mprefix))
                WITH s, relationship, t,
                     vector.similarity.cosine(relationship.description_embedding, $embedding) AS score
                WHERE score >= $threshold
                RETURN s.id as source_id, s.name as source_name,
                       t.id as target_id, t.name as target_name,
                       relationship.description as description,
                       relationship.strength as strength,
                       relationship.source_segment_ids as source_segment_ids,
                       score
                ORDER BY score DESC
                LIMIT $top_k
            """, {'gid': group_id, 'mid': media_id, 'mprefix': f"media_{media_id}_",
                  'top_k': top_k, 'embedding': embedding, 'threshold': threshold})
            return [dict(r) for r in result]

    def search_segments_fulltext(self, query: str, group_id: str, top_k: int = 5, media_id: int = None) -> List[Dict]:
        """片段全文检索（BM25，group 内闭包，media_id 可选限定单视频）"""
        with self.driver.session() as session:
            result = session.run("""
                CALL db.index.fulltext.queryNodes('segment_fulltext', $query)
                YIELD node, score
                WHERE node.group_id = $gid
                  AND ($mid IS NULL OR node.media_id = $mid)
                RETURN node, score
                ORDER BY score DESC
                LIMIT $top_k
            """, {'query': self._escape_lucene(query), 'gid': group_id, 'mid': media_id, 'top_k': top_k})
            return [{'segment': dict(r['node']), 'score': r['score']} for r in result]

    def search_communities_fulltext(self, query: str, group_id: str, top_k: int = 5, media_id: int = None) -> List[Dict]:
        """社区全文检索（BM25，group 内闭包，media_id 可选限定单视频）"""
        with self.driver.session() as session:
            result = session.run("""
                CALL db.index.fulltext.queryNodes('community_fulltext', $query)
                YIELD node, score
                WHERE node.group_id = $gid
                  AND ($mid IS NULL OR EXISTS { (node)-[:CONTAINS]->(:Segment {media_id: $mid}) })
                RETURN node, score
                ORDER BY score DESC
                LIMIT $top_k
            """, {'query': self._escape_lucene(query), 'gid': group_id, 'mid': media_id, 'top_k': top_k})
            return [{'community': dict(r['node']), 'score': r['score']} for r in result]

    def entity_bfs_search(self, origin_ids: List[str], group_id: str, depth: int = 2, limit: int = 10,
                          media_id: int = None) -> List[Dict]:
        """
        BFS 图遍历召回（Graphiti 式）：从起点实体出发沿 RELATES_TO 扩散，返回发现的实体
        起点通常是其他召回路命中的实体
        """
        if not origin_ids:
            return []
        depth = max(1, min(int(depth), 3))  # 限制深度，防止全图扩散
        with self.driver.session() as session:
            result = session.run(f"""
                MATCH (origin:Entity) WHERE origin.id IN $origin_ids
                MATCH path = (origin)-[:RELATES_TO*1..{depth}]-(e:Entity)
                WHERE NOT e.id IN $origin_ids AND e.group_id = $gid
                  AND ($mid IS NULL OR EXISTS {{ (e)-[:MENTIONED_IN]->(:Segment {{media_id: $mid}}) }})
                WITH e, min(length(path)) as distance
                RETURN e, distance
                ORDER BY distance ASC
                LIMIT $limit
            """, {'origin_ids': origin_ids, 'gid': group_id, 'mid': media_id, 'limit': limit})
            return [{'entity': dict(r['e']), 'score': 1.0 / r['distance']} for r in result]

    def get_entity_relationships(self, entity_id: str, top_k: int = 5) -> List[Dict]:
        """获取实体的一跳关系（按强度排序）"""
        with self.driver.session() as session:
            result = session.run("""
                MATCH (e:Entity {id: $entity_id})-[r:RELATES_TO]-(other:Entity)
                RETURN e.name as source_name, other.name as target_name,
                       other.id as other_id, other.description as other_description,
                       r.description as description, r.strength as strength,
                       r.source_segment_ids as source_segment_ids,
                       startNode(r).id as start_id
                ORDER BY r.strength DESC
                LIMIT $top_k
            """, {'entity_id': entity_id, 'top_k': top_k})

            return [dict(record) for record in result]

    def get_community_segments(self, community_id: str) -> List[Dict]:
        """获取社区关联的片段（追溯）"""
        with self.driver.session() as session:
            result = session.run("""
                MATCH (c:Community {id: $community_id})-[:CONTAINS]->(s:Segment)
                RETURN s
                ORDER BY s.media_id, s.segment_index
            """, {'community_id': community_id})

            return [dict(record['s']) for record in result]

    def get_entity_sources(self, entity_id: str) -> List[Dict]:
        """获取实体来源（追溯）"""
        with self.driver.session() as session:
            result = session.run("""
                MATCH (e:Entity {id: $entity_id})-[r:MENTIONED_IN]->(s:Segment)
                RETURN s, r
                ORDER BY s.start_ms
            """, {'entity_id': entity_id})

            return [
                {
                    'segment': dict(record['s']),
                    'confidence': record['r']['confidence'],
                    'source_type': record['r']['source_type']
                }
                for record in result
            ]

    def get_media_communities(self, media_id: int) -> List[Dict]:
        """获取视频的所有社区"""
        with self.driver.session() as session:
            result = session.run("""
                MATCH (c:Community)-[:CONTAINS]->(s:Segment {media_id: $media_id})
                RETURN DISTINCT c
            """, {'media_id': media_id})

            return [dict(record['c']) for record in result]
