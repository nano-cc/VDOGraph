#!/usr/bin/env python3
"""
两跳 commit 直接测试（不走 MQ/Java，直接用已解析视频的 raw_extraction）
- 用例 1：media 7 @ user_2（该视频已建图）→ 验证"合并到已有"（幂等）
- 用例 2：media 7 @ user_999（空 group）→ 验证"新建"
用法：python3 experiment/test_two_hop_commit.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def run_case(media_id: int, group_id: str, do_apply: bool):
    from app.services.video_processing_pipeline import VideoProcessingPipeline

    pipeline = VideoProcessingPipeline()
    print(f"\n{'='*64}\n用例：media_id={media_id} group={group_id} apply={do_apply}")

    # prepare（锁外）
    plan = await pipeline.prepare_commit_two_hop(media_id, group_id)
    stats = plan['stats']
    print(f"\nprepare 统计: {stats}")
    merges = [r for r in plan['resolutions'] if r.action == 'merge']
    creates = [r for r in plan['resolutions'] if r.action == 'create']
    print(f"决议分布：并入已有 {len(merges)} / 新建 {len(creates)}")
    print(f"写集大小（锁数量）: {len(plan['write_set'])}")
    print(f"关系：{len(plan['final_relations'])} 条（丢弃 {plan['relations_dropped']}）")

    print("\nmerge 样例（并入已有实体）:")
    for r in merges[:8]:
        print(f"  {r.group.name}（{'/'.join(r.group.surface_forms[:3])}）→ {r.target_entity_id} [{r.via}]")
    print("\ncreate 样例（新建实体）:")
    for r in creates[:8]:
        print(f"  {r.chosen_name} → {r.target_entity_id}")

    if not do_apply:
        print("\n（prepare-only，跳过 apply）")
        return

    # apply（细粒度锁）
    result = await pipeline.apply_commit_two_hop(plan)
    print(f"\napply 结果: {result['statistics']}")

    # 验证：实体数/关系数落库核对
    from app.clients.neo4j_client import Neo4jClient
    client = Neo4jClient()
    with client.driver.session() as s:
        cnt = s.run("""
            MATCH (e:Entity {group_id: $gid})-[:MENTIONED_IN]->(sg:Segment {media_id: $mid})
            RETURN count(DISTINCT e) AS c
        """, gid=group_id, mid=media_id).single()['c']
        rel_cnt = s.run("""
            MATCH ()-[r:RELATES_TO {group_id: $gid}]->()
            WHERE ANY(sid IN r.source_segment_ids WHERE sid STARTS WITH $prefix)
            RETURN count(r) AS c
        """, gid=group_id, prefix=f"media_{media_id}_").single()['c']
        comm = s.run("""
            MATCH (e:Entity {group_id: $gid})-[:MENTIONED_IN]->(sg:Segment {media_id: $mid})
            WITH DISTINCT e
            MATCH (e)-[:BELONGS_TO]->(c:Community)
            RETURN count(e) AS c
        """, gid=group_id, mid=media_id).single()['c']
    client.close()
    print(f"落库核对：本视频提及实体 {cnt}，关系 {rel_cnt}，已入社区实体 {comm}")


async def main():
    # 支持命令行：python3 test_two_hop_commit.py <media_id> <group_id>
    if len(sys.argv) >= 3:
        await run_case(int(sys.argv[1]), sys.argv[2], do_apply=True)
        return
    # 用例 1：已建图 group，验证合并（幂等重跑）
    await run_case(7, "user_2", do_apply=True)
    # 用例 2：空 group，验证新建
    await run_case(7, "user_999", do_apply=True)


if __name__ == '__main__':
    asyncio.run(main())
