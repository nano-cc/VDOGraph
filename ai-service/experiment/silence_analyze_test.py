"""静音感知分片 analyze 单测：只跑 analyze 阶段，验证分片 + LLM 抽取 + raw_extraction 落库"""
import asyncio
import json
import sys

sys.path.insert(0, '/mnt/Data/projs/Java/DOVideo-AI/ai-service')

MEDIA_ID = 9999
GROUP_ID = 'user_2'
VIDEO = '/tmp/seg_test_4min.mp4'


async def main():
    from app.services.video_processing_pipeline import VideoProcessingPipeline
    pipeline = VideoProcessingPipeline()

    async def progress(done, total):
        print(f'[PROGRESS] {done}/{total}', flush=True)

    result = await pipeline.analyze(VIDEO, MEDIA_ID, progress_cb=progress, group_id=GROUP_ID)
    stats = result.get('statistics')
    print(f'[RESULT] statistics: {stats}', flush=True)

    # 检查落库的片段与 raw_extraction
    segs = pipeline.neo4j_client.get_media_segments(MEDIA_ID)
    print(f'\n[SEGMENTS] {len(segs)} 段:', flush=True)
    for s in segs:
        dur = (s['end_ms'] - s['start_ms']) / 1000
        print(f"  - id={s['id']} core=[{s['start_ms']/1000:.1f}s,{s['end_ms']/1000:.1f}s) "
              f"dur={dur:.1f}s transcript_chars={len(s.get('transcript') or '')} "
              f"ocr={len(s.get('ocr_texts') or [])} frames={len(s.get('frame_urls') or [])}", flush=True)

    # raw_extraction 存在每个 Segment 节点上（JSON 字符串）
    all_ents, all_rels = [], []
    missing = 0
    for s in segs:
        raw_str = s.get('raw_extraction')
        if not raw_str:
            missing += 1
            continue
        raw = json.loads(raw_str)
        all_ents.extend(raw.get('entities', []))
        all_rels.extend(raw.get('relationships', []))
    print(f'\n[RAW_EXTRACTION] entities={len(all_ents)} relationships={len(all_rels)} '
          f'(缺抽取的片段: {missing})', flush=True)
    from collections import Counter
    if all_ents:
        print('  实体类型分布:', dict(Counter(e.get('type', '?') for e in all_ents)), flush=True)
        for e in all_ents[:8]:
            print(f"    · {e.get('name')} ({e.get('type')})", flush=True)


if __name__ == '__main__':
    asyncio.run(main())
