"""
视频解析服务
实现与 Java 端一致的视频解析逻辑：
1. FFmpeg 音视频分离
2. ASR 转写（阿里云 ASR）
3. 关键帧提取（FFmpeg 场景检测）
4. OCR 识别（Tesseract）
5. 对齐成 VideoSegment
"""
import subprocess
import re
import asyncio
from pathlib import Path
from typing import List, Optional
from PIL import Image
import tempfile
import uuid

from app.models.video import VideoSegment, VideoContext
from app.clients.aliyun_asr import AliyunAsrClient
from app.clients.ocr import OcrClient
from app.clients.minio import MinioClient
from app.core.logging import logger


class VideoParser:
    # 与 Java 端保持一致的常量
    SEGMENT_MS = 60_000  # 60 秒
    FALLBACK_FRAME_INTERVAL_MS = 30_000  # 30 秒
    EVIDENCE_OBJECT_PREFIX = "evidence-frames"
    ASR_CONCURRENCY = 4   # ASR 并发数
    OCR_CONCURRENCY = 6   # OCR 并发数（本地 CPU 子进程）

    # ---- 静音感知分片（docs：静音点滑动窗口 + 硬切 overlap 补偿） ----
    TARGET_SEGMENT_MS = 60_000    # 目标段长
    MIN_SEGMENT_MS = 30_000       # 最小段长（过短语义太薄）
    MAX_SEGMENT_MS = 90_000       # 最大段长（LLM 上下文/抽取质量上限）
    OVERLAP_MS = 8_000            # 硬切补偿：切点两侧音频各延伸 8s（静音切不补偿）
    SILENCE_NOISE_DB = -35        # 低于此分贝视为静音
    SILENCE_MIN_DURATION_S = 0.4  # 静音最短持续（过滤正常换气）
    CUT_SILENCE = 'silence'
    CUT_HARD = 'hard'

    def __init__(self):
        self.asr_client = AliyunAsrClient()
        self.ocr_client = OcrClient()
        self.minio_client = MinioClient()

    async def parse(self, video_path: str, user_goal: str = "", media_id: Optional[int] = None) -> VideoContext:
        """
        视频解析主函数
        """
        import time
        start_time = time.time()

        logger.info(f"[VIDEO_PARSE] Starting video parsing: {video_path}")
        logger.info(f"[VIDEO_PARSE] User goal: {user_goal}")

        # 创建工作目录
        work_dir = Path(tempfile.gettempdir()) / f"video-context-{uuid.uuid4()}"
        work_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"[VIDEO_PARSE] Work directory: {work_dir}")

        try:
            # 1. ASR 分支（音频）
            logger.info(f"[VIDEO_PARSE] Step 1: ASR transcription")
            asr_start = time.time()
            transcript_segments, seg_plans = await self._transcribe(video_path, work_dir / "audio")
            asr_duration = (time.time() - asr_start) * 1000
            logger.info(f"[VIDEO_PARSE] ASR completed: {len(transcript_segments)} segments, {asr_duration:.2f}ms")

            # 2. OCR 分支（视频）
            logger.info(f"[VIDEO_PARSE] Step 2: OCR extraction")
            ocr_start = time.time()
            frame_parts = await self._extract_keyframes(video_path, work_dir / "frames", media_id)
            ocr_duration = (time.time() - ocr_start) * 1000
            logger.info(f"[VIDEO_PARSE] OCR completed: {len(frame_parts)} keyframes, {ocr_duration:.2f}ms")

            # 3. 对齐成 VideoSegment
            logger.info(f"[VIDEO_PARSE] Step 3: Merging segments")
            merge_start = time.time()
            segments = self._merge(transcript_segments, frame_parts, seg_plans)
            merge_duration = (time.time() - merge_start) * 1000
            logger.info(f"[VIDEO_PARSE] Merge completed: {len(segments)} segments, {merge_duration:.2f}ms")

            if not segments:
                raise ValueError("视频未解析出有效语音或画面文字")

            total_duration = (time.time() - start_time) * 1000
            logger.info(f"[VIDEO_PARSE] Video parsing completed: {len(segments)} segments, {total_duration:.2f}ms")

            return VideoContext(
                source=video_path,
                user_goal=user_goal,
                segments=segments
            )

        except Exception as e:
            total_duration = (time.time() - start_time) * 1000
            logger.error(f"[VIDEO_PARSE] Video parsing failed after {total_duration:.2f}ms: {e}", exc_info=True)
            raise

        finally:
            # 清理工作目录
            logger.info(f"[VIDEO_PARSE] Cleaning up work directory: {work_dir}")
            self._delete_directory(work_dir)

    async def _transcribe(self, video_path: str, audio_dir: Path):
        """
        ASR 转写（静音感知分片版）：
        1. silencedetect 拿全部静音点
        2. 游标走查切分核心区间（静音优先，无候选硬切；[30s,90s] 窗口，目标 60s）
        3. 过短合并/过长重切规整
        4. 音频区间 = 核心区间 + 硬切侧各延 8s（静音切不补偿）
        5. 全程失败/无静音 → 回退固定 60s 硬切 + 双侧重叠（原方案保底）
        返回 (transcripts, plans)：transcripts 带真实核心区间起止，plans 供 _merge 帧归属
        """
        import time
        start_time = time.time()
        logger.info(f"[ASR] Starting transcription: {video_path}")

        audio_dir.mkdir(parents=True, exist_ok=True)

        duration_ms = self._probe_duration_ms(video_path)
        silence_points = self._detect_silences(video_path) if duration_ms else []
        plans = self._plan_segments(duration_ms, silence_points) if duration_ms else None
        if not plans:
            logger.warning("[ASR] 静音感知分片不可用，回退固定 60s 分片")
            plans = self._fallback_fixed_plans(duration_ms)

        hard_cuts = sum(1 for p in plans if p['left_cut'] == self.CUT_HARD or p['right_cut'] == self.CUT_HARD)
        logger.info(f"[ASR] 分片计划：{len(plans)} 段（静音切 {len(plans)-hard_cuts}，硬切补偿 {hard_cuts}）")

        # 按计划逐段提取音频（含 overlap 延伸）
        logger.info(f"[ASR] Running FFmpeg to extract {len(plans)} audio segments")
        ffmpeg_start = time.time()
        audio_files = []
        for i, plan in enumerate(plans):
            audio_file = audio_dir / f"audio_{i:03d}.mp3"
            self._run_ffmpeg([
                "ffmpeg", "-y",
                "-ss", f"{plan['audio_start_ms'] / 1000:.3f}",
                "-to", f"{plan['audio_end_ms'] / 1000:.3f}",
                "-i", video_path,
                "-vn", "-acodec", "libmp3lame",
                str(audio_file)
            ])
            audio_files.append(audio_file)
        ffmpeg_duration = (time.time() - ffmpeg_start) * 1000
        logger.info(f"[ASR] FFmpeg completed in {ffmpeg_duration:.2f}ms")

        semaphore = asyncio.Semaphore(self.ASR_CONCURRENCY)

        async def transcribe_one(i: int, audio_file: Path):
            async with semaphore:
                segment_start = time.time()
                text = await asyncio.to_thread(self.asr_client.audio_to_text_sync, str(audio_file))
                segment_duration = (time.time() - segment_start) * 1000
                logger.info(f"[ASR] Segment {i} completed in {segment_duration:.2f}ms, text length: {len(text) if text else 0}")
                return i, text

        tasks = [transcribe_one(i, f) for i, f in enumerate(audio_files)]
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)

        result = []
        failed_segments = 0
        last_error = None
        for i, outcome in enumerate(outcomes):
            if isinstance(outcome, Exception):
                failed_segments += 1
                last_error = outcome
                logger.warning(f"[ASR] Segment {i} failed: error={outcome}")
                continue
            idx, text = outcome
            if text and text.strip():
                result.append({
                    'start_ms': plans[idx]['start_ms'],
                    'end_ms': plans[idx]['end_ms'],
                    'text': text
                })

        result.sort(key=lambda x: x['start_ms'])

        if not result and failed_segments > 0:
            raise ValueError(f"所有 ASR 分片均处理失败: {last_error}")

        total_duration = (time.time() - start_time) * 1000
        logger.info(f"[ASR] Transcription completed: {len(result)} segments, {failed_segments} failed, {total_duration:.2f}ms")

        return result, plans

    def _probe_duration_ms(self, video_path: str) -> Optional[int]:
        """ffprobe 读视频时长（毫秒）"""
        try:
            process = subprocess.run([
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                video_path
            ], capture_output=True, text=True, timeout=30)
            duration = process.stdout.strip()
            if duration and duration != "N/A":
                return int(float(duration) * 1000)
        except Exception as e:
            logger.warning(f"[ASR] ffprobe duration failed: {e}")
        return None

    def _detect_silences(self, video_path: str) -> List[int]:
        """ffmpeg silencedetect：返回全部静音区间中点（毫秒）"""
        try:
            process = subprocess.run([
                "ffmpeg", "-i", video_path,
                "-af", f"silencedetect=noise={self.SILENCE_NOISE_DB}dB:d={self.SILENCE_MIN_DURATION_S}",
                "-f", "null", "-"
            ], capture_output=True, text=True, timeout=600)
            output = process.stdout + process.stderr

            points = []
            start = None
            for line in output.splitlines():
                m_start = re.search(r"silence_start: ([0-9.]+)", line)
                m_end = re.search(r"silence_end: ([0-9.]+)", line)
                if m_start:
                    start = float(m_start.group(1))
                elif m_end and start is not None:
                    end = float(m_end.group(1))
                    points.append(int((start + end) / 2 * 1000))
                    start = None
            logger.info(f"[ASR] silencedetect: {len(points)} 个静音点")
            return points
        except Exception as e:
            logger.warning(f"[ASR] silencedetect failed: {e}")
            return []

    def _plan_segments(self, duration_ms: int, silence_points: List[int]) -> List[dict]:
        """
        游标走查 + 规整，产出分片计划。
        每段：{start_ms, end_ms, audio_start_ms, audio_end_ms, left_cut, right_cut}
        切点类型：silence（静音点，不加 overlap）/ hard（硬切，两侧加 overlap）
        """
        if not duration_ms or duration_ms <= 0:
            return []

        # ---- 游标走查 ----
        cuts = []  # (ms, type)
        cursor = 0
        while cursor + self.MIN_SEGMENT_MS < duration_ms:
            lo = cursor + self.MIN_SEGMENT_MS
            hi = min(cursor + self.MAX_SEGMENT_MS, duration_ms)
            target = cursor + self.TARGET_SEGMENT_MS

            candidates = [c for c in silence_points if lo < c <= hi]
            if candidates:
                cut = min(candidates, key=lambda c: abs(c - target))
                cuts.append((cut, self.CUT_SILENCE))
            else:
                cuts.append((min(target, hi), self.CUT_HARD))
            cursor = cuts[-1][0]

            # 尾部不足 MIN 就不再切，并进最后一段
            if duration_ms - cursor < self.MIN_SEGMENT_MS:
                break

        boundaries = [0] + [c for c, _ in cuts] + [duration_ms]
        cut_types = [t for _, t in cuts]
        segments = [{'start_ms': boundaries[i], 'end_ms': boundaries[i + 1],
                     'left_cut': 'edge' if i == 0 else cut_types[i - 1],
                     'right_cut': 'edge' if i == len(boundaries) - 2 else cut_types[i]}
                    for i in range(len(boundaries) - 1)]

        # ---- 过短合并（静音太密切出碎片）----
        merged = []
        for seg in segments:
            if merged and seg['end_ms'] - seg['start_ms'] < self.MIN_SEGMENT_MS:
                prev = merged[-1]
                prev['end_ms'] = seg['end_ms']
                prev['right_cut'] = seg['right_cut']  # 继承右边界类型
            else:
                merged.append(dict(seg))
        segments = merged

        # ---- 过长重切（合并导致超限）----
        final = []
        for seg in segments:
            while seg['end_ms'] - seg['start_ms'] > self.MAX_SEGMENT_MS:
                mid = seg['start_ms'] + (seg['end_ms'] - seg['start_ms']) // 2
                lo, hi = seg['start_ms'] + self.MIN_SEGMENT_MS, seg['end_ms'] - self.MIN_SEGMENT_MS
                candidates = [c for c in silence_points if lo < c < hi]
                if candidates:
                    cut = min(candidates, key=lambda c: abs(c - mid))
                    ctype = self.CUT_SILENCE
                else:
                    cut = max(lo, min(mid, hi))
                    ctype = self.CUT_HARD
                final.append({'start_ms': seg['start_ms'], 'end_ms': cut,
                              'left_cut': seg['left_cut'], 'right_cut': ctype})
                seg = {'start_ms': cut, 'end_ms': seg['end_ms'],
                       'left_cut': ctype, 'right_cut': seg['right_cut']}
            final.append(seg)

        # ---- 音频区间 = 核心 + 硬切侧延伸 ----
        for seg in final:
            left_overlap = self.OVERLAP_MS if seg['left_cut'] == self.CUT_HARD else 0
            right_overlap = self.OVERLAP_MS if seg['right_cut'] == self.CUT_HARD else 0
            seg['audio_start_ms'] = max(0, seg['start_ms'] - left_overlap)
            seg['audio_end_ms'] = min(duration_ms, seg['end_ms'] + right_overlap)

        return final

    def _fallback_fixed_plans(self, duration_ms: Optional[int]) -> List[dict]:
        """回退：固定 60s 硬切 + 双侧重叠（原方案的保底形态）"""
        if not duration_ms or duration_ms <= 0:
            raise ValueError("无法获取视频时长，分片失败")
        plans = []
        cursor = 0
        while cursor < duration_ms:
            end = min(cursor + self.SEGMENT_MS, duration_ms)
            plans.append({
                'start_ms': cursor, 'end_ms': end,
                'left_cut': 'edge' if cursor == 0 else self.CUT_HARD,
                'right_cut': 'edge' if end >= duration_ms else self.CUT_HARD,
                'audio_start_ms': max(0, cursor - (self.OVERLAP_MS if cursor > 0 else 0)),
                'audio_end_ms': min(duration_ms, end + (self.OVERLAP_MS if end < duration_ms else 0)),
            })
            cursor = end
        return plans

    async def _extract_keyframes(self, video_path: str, frame_dir: Path, media_id: Optional[int] = None) -> List[dict]:
        """
        关键帧提取 + OCR（哈希去重串行，OCR+上传并发）
        与 Java 端 VideoContextService.extractKeyFrames() 保持一致
        """
        import time
        start_time = time.time()

        logger.info(f"[OCR] Starting keyframe extraction: {video_path}")

        frame_dir.mkdir(parents=True, exist_ok=True)

        # FFmpeg 场景检测（与 Java 端一致）
        logger.info(f"[OCR] Running FFmpeg to extract keyframes")
        ffmpeg_start = time.time()
        timestamps = []
        self._run_ffmpeg_with_timestamps([
            "ffmpeg", "-y", "-i", video_path,
            "-vf", "select=eq(n\\,0)+gt(scene\\,0.35)+gte(t-prev_selected_t\\,30),showinfo",
            "-vsync", "vfr",
            str(frame_dir / "frame_%06d.jpg")
        ], timestamps)
        ffmpeg_duration = (time.time() - ffmpeg_start) * 1000
        logger.info(f"[OCR] FFmpeg completed in {ffmpeg_duration:.2f}ms")

        # 获取所有关键帧
        frame_files = sorted(frame_dir.glob("frame_*.jpg"))
        logger.info(f"[OCR] Found {len(frame_files)} keyframes")

        # 感知哈希去重（串行，链式依赖前一帧的哈希，毫秒级）
        kept_frames = []
        previous_hash = None
        skipped_frames = 0
        for i, frame_file in enumerate(frame_files):
            image_hash = self._difference_hash(frame_file)
            if previous_hash is not None and bin(previous_hash ^ image_hash).count('1') <= 5:
                skipped_frames += 1
                continue
            previous_hash = image_hash
            timestamp_ms = timestamps[i] if i < len(timestamps) else i * self.FALLBACK_FRAME_INTERVAL_MS
            kept_frames.append((i, frame_file, timestamp_ms))

        logger.info(f"[OCR] {len(kept_frames)} frames kept after dedup, {skipped_frames} skipped, concurrency={self.OCR_CONCURRENCY}")

        # OCR + 上传并发（to_thread 真并发）
        # 帧按 media_id 命名空间，避免跨视频覆盖
        frame_prefix = f"{self.EVIDENCE_OBJECT_PREFIX}/{media_id}" if media_id is not None else self.EVIDENCE_OBJECT_PREFIX
        semaphore = asyncio.Semaphore(self.OCR_CONCURRENCY)

        async def process_one(i: int, frame_file: Path, timestamp_ms: int):
            async with semaphore:
                # OCR 识别
                ocr_start = time.time()
                ocr_text = await asyncio.to_thread(self.ocr_client.recognize_sync, str(frame_file))
                ocr_duration = (time.time() - ocr_start) * 1000
                logger.info(f"[OCR] Frame {i} OCR completed in {ocr_duration:.2f}ms, text length: {len(ocr_text) if ocr_text else 0}")

                # 上传到 MinIO
                try:
                    frame_url = await asyncio.to_thread(
                        self.minio_client.upload_file_sync,
                        str(frame_file), frame_file.name, frame_prefix
                    )
                except Exception as e:
                    logger.warning(f"[OCR] Frame {i} upload failed: {frame_file.name}, error={e}")
                    frame_url = f"{video_path}#timestampMs={timestamp_ms}"

                return {
                    'timestamp_ms': timestamp_ms,
                    'ocr_text': ocr_text,
                    'frame_url': frame_url
                }

        tasks = [process_one(i, f, ts) for i, f, ts in kept_frames]
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)

        result = []
        failed_frames = 0
        for outcome in outcomes:
            if isinstance(outcome, Exception):
                failed_frames += 1
                logger.warning(f"[OCR] Frame processing failed: {outcome}")
            else:
                result.append(outcome)

        result.sort(key=lambda x: x['timestamp_ms'])

        if not result and failed_frames > 0:
            raise ValueError("所有 OCR 关键帧均处理失败")

        total_duration = (time.time() - start_time) * 1000
        logger.info(f"[OCR] Keyframe extraction completed: {len(result)} frames, {failed_frames} failed, {skipped_frames} skipped, {total_duration:.2f}ms")

        return result

    def _merge(self, transcripts: List[dict], frames: List[dict], plans: List[dict]) -> List[VideoSegment]:
        """
        对齐 ASR 和 OCR（静音感知分片版）：
        - 片段 = 分片计划的核心区间（不再按 60s 网格）
        - 关键帧按核心区间 [start, end) 唯一归属（不考虑 overlap，不重复）
        - 尾帧（>= 最后段 end）归最后一段；空帧片段取最近帧兜底
        """
        by_start = {t['start_ms']: t for t in transcripts}
        segments = []

        for idx, plan in enumerate(plans):
            t = by_start.get(plan['start_ms'])
            transcript_text = t['text'] if t else ''
            segments.append({
                'start_ms': plan['start_ms'],
                'end_ms': plan['end_ms'],
                'transcript': transcript_text,
                'ocr_texts': [],
                'evidence_frames': [],
            })

        # 关键帧唯一归属（核心区间互不重叠且连续铺满全片）
        for frame in frames:
            assigned = False
            for seg in segments:
                if seg['start_ms'] <= frame['timestamp_ms'] < seg['end_ms']:
                    if frame['ocr_text'] and frame['ocr_text'].strip():
                        seg['ocr_texts'].append(frame['ocr_text'])
                    seg['evidence_frames'].append(frame['frame_url'])
                    assigned = True
                    break
            if not assigned and segments:
                # 尾帧兜底：归最后一段
                seg = segments[-1]
                if frame['ocr_text'] and frame['ocr_text'].strip():
                    seg['ocr_texts'].append(frame['ocr_text'])
                seg['evidence_frames'].append(frame['frame_url'])

        # 空帧兜底：没有帧的片段取时间最近的帧（引用展示用，不改变归属唯一性）
        if frames:
            for seg in segments:
                if not seg['evidence_frames']:
                    mid = (seg['start_ms'] + seg['end_ms']) // 2
                    nearest = min(frames, key=lambda f: abs(f['timestamp_ms'] - mid))
                    seg['evidence_frames'].append(nearest['frame_url'])

        # 过滤完全空片段（无文本无帧）：与原行为一致（只有有内容的窗口才产出）
        result = [
            VideoSegment(
                start_ms=seg['start_ms'],
                end_ms=seg['end_ms'],
                transcript=seg['transcript'],
                ocr_texts=seg['ocr_texts'],
                evidence_frames=seg['evidence_frames']
            )
            for seg in segments
            if seg['transcript'] or seg['ocr_texts'] or seg['evidence_frames']
        ]
        return result

    def _run_ffmpeg(self, command: List[str]):
        """运行 FFmpeg 命令"""
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        process.wait(timeout=900)  # 15 分钟超时

        if process.returncode != 0:
            raise RuntimeError("FFmpeg 执行失败")

    def _run_ffmpeg_with_timestamps(self, command: List[str], timestamps: List[int]):
        """运行 FFmpeg 命令并提取时间戳"""
        log_file = Path(tempfile.gettempdir()) / f"ffmpeg-{uuid.uuid4()}.log"

        with open(log_file, 'w') as f:
            process = subprocess.Popen(
                command,
                stdout=f,
                stderr=subprocess.STDOUT
            )
            process.wait(timeout=900)  # 15 分钟超时

        if process.returncode != 0:
            log_file.unlink(missing_ok=True)
            raise RuntimeError("FFmpeg 执行失败")

        # 提取时间戳
        pts_time_pattern = re.compile(r"pts_time:([0-9.]+)")
        with open(log_file, 'r') as f:
            for line in f:
                if "showinfo" in line:
                    match = pts_time_pattern.search(line)
                    if match:
                        timestamps.append(int(float(match.group(1)) * 1000))

        log_file.unlink(missing_ok=True)

    def _difference_hash(self, image_path: Path) -> int:
        """
        感知哈希（与 Java 端一致）
        """
        img = Image.open(image_path).convert('L').resize((9, 8), Image.LANCZOS)
        pixels = list(img.getdata())

        hash_value = 0
        for y in range(8):
            for x in range(8):
                hash_value <<= 1
                if pixels[y * 9 + x] > pixels[y * 9 + x + 1]:
                    hash_value |= 1

        return hash_value

    def _delete_directory(self, directory: Path):
        """删除目录"""
        if not directory.exists():
            return

        for path in sorted(directory.rglob('*'), reverse=True):
            try:
                if path.is_file():
                    path.unlink()
                elif path.is_dir():
                    path.rmdir()
            except Exception as e:
                logger.warning(f"Failed to delete {path}: {e}")

        try:
            directory.rmdir()
        except Exception as e:
            logger.warning(f"Failed to delete directory {directory}: {e}")
