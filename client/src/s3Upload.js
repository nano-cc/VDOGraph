/**
 * S3 multipart 直传上传
 * 流程：初验（后缀+魔数+大小）→ xxHash64 指纹 → init（秒传/续传/新建）
 *      → presign → PUT 直传 MinIO → complete（服务端组装+ffprobe）
 * 断点续传：quickHash 找回任务 + ListParts 只补缺片
 */
import { createXXHash128 } from 'hash-wasm'
import { apiRequest } from './api'

export const S3_CHUNK_SIZE = 5 * 1024 * 1024  // S3 multipart 最小分片
const PRESIGN_BATCH = 10  // 每批签发多少个分片 URL

/**
 * XXH3-128 全文件指纹（流式，大文件不爆内存；实测吞吐 ~3GB/s）
 * 每次调用新建实例：并发上传多个文件时哈希状态不互串
 */
export async function computeQuickHash(file) {
  const hasher = await createXXHash128(0)
  const SLICE = 16 * 1024 * 1024
  for (let offset = 0; offset < file.size; offset += SLICE) {
    const buf = await file.slice(offset, Math.min(offset + SLICE, file.size)).arrayBuffer()
    hasher.update(new Uint8Array(buf))
  }
  return hasher.digest('hex')  // 32 位十六进制
}

/** 魔数校验：读头部 16 字节识别真实容器（防改名伪装） */
export async function checkMagicBytes(file) {
  const buf = new Uint8Array(await file.slice(0, 16).arrayBuffer())
  const ascii = i => String.fromCharCode(buf[i])
  // MP4/MOV: 偏移 4 处是 "ftyp"
  if (ascii(4) === 'f' && ascii(5) === 't' && ascii(6) === 'y' && ascii(7) === 'p') return true
  // MKV/WebM: EBML 头 0x1A45DFA3
  if (buf[0] === 0x1a && buf[1] === 0x45 && buf[2] === 0xdf && buf[3] === 0xa3) return true
  // AVI: "RIFF....AVI "
  if (ascii(0) === 'R' && ascii(1) === 'I' && ascii(2) === 'F' && ascii(3) === 'F' &&
      ascii(8) === 'A' && ascii(9) === 'V' && ascii(10) === 'I') return true
  return false
}

export function validateVideoFileV2(file) {
  const ext = file.name.split('.').pop()?.toLowerCase()
  const allowed = ['mp4', 'mov', 'mkv', 'avi', 'webm', 'm4v']
  if (!allowed.includes(ext)) return `不支持的视频格式 .${ext}（支持：${allowed.join('/')}）`
  if (!file.size) return '该文件大小为 0，可能已损坏'
  const MAX_BYTES = 2048 * 1024 * 1024
  if (file.size > MAX_BYTES) return `文件 ${(file.size / 1024 / 1024 / 1024).toFixed(1)}GB，超过 2GB 上限，请先压缩`
  return ''
}

export class UploadAbortedError extends Error {
  constructor() { super('上传已取消') }
}

/** 单片预签名（重签重试用） */
async function presignOne(uploadId, partNumber, signal) {
  const params = new URLSearchParams({ uploadId })
  params.append('partNumbers', partNumber)
  const res = await apiRequest(`/media/v2/presign?${params}`, { method: 'POST', signal })
  if (!res.ok) throw new Error('分片签名失败')
  const urls = await res.json()
  return urls[partNumber]
}

/**
 * 分片 PUT 上传（带重试与过期重签，H3+G3）
 * - 普通网络失败：退避重试 3 次（0.8s/1.6s）
 * - 403/400（签名过期特征）：自动重新签名后重试
 */
async function putPartWithRetry(uploadId, partNumber, blob, signal) {
  const maxAttempts = 3
  let url = await presignOne(uploadId, partNumber, signal)
  let lastError = null
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      const resp = await fetch(url, { method: 'PUT', body: blob, signal })
      if (resp.ok) return
      if ((resp.status === 403 || resp.status === 400) && attempt < maxAttempts) {
        // 签名过期：重签后用新 URL 重试
        url = await presignOne(uploadId, partNumber, signal)
        continue
      }
      throw new Error(`分片 ${partNumber} 上传失败 (HTTP ${resp.status})`)
    } catch (error) {
      if (error?.name === 'AbortError') throw error
      lastError = error
      if (attempt < maxAttempts) {
        await new Promise(r => setTimeout(r, 800 * (2 ** (attempt - 1))))
      }
    }
  }
  throw lastError || new Error(`分片 ${partNumber} 上传失败`)
}

/**
 * 直传上传主流程
 * @returns {Promise<{mediaId, instant}>}
 */
export async function uploadVideoS3(file, { onProgress = () => {}, signal } = {}) {
  // 1. 初验
  const invalid = validateVideoFileV2(file)
  if (invalid) throw new Error(invalid)
  if (!(await checkMagicBytes(file))) {
    throw new Error('文件内容不是有效视频（可能伪装了后缀名）')
  }

  // 2. 指纹 + init
  onProgress({ phase: 'hashing', label: '计算文件指纹…' })
  const quickHash = await computeQuickHash(file)
  const totalChunks = Math.ceil(file.size / S3_CHUNK_SIZE)

  onProgress({ phase: 'init', label: '检查是否已上传过…' })
  const initRes = await apiRequest(
    `/media/v2/init-upload?filename=${encodeURIComponent(file.name)}&size=${file.size}&totalChunks=${totalChunks}&quickHash=${quickHash}`,
    { method: 'POST', signal }
  )
  if (!initRes.ok) throw new Error(await initRes.text() || '初始化上传失败')
  const initData = await initRes.json()

  if (initData.status === 'INSTANT') {
    onProgress({ phase: 'done', label: '已上传过，秒传成功', percent: 100 })
    return { mediaId: initData.mediaId, instant: true }
  }

  const uploadId = initData.uploadId
  const uploadedSet = new Set((initData.uploadedParts || []).map(p => p.partNumber))
  const abortController = { aborted: false }

  try {
    // 3. 逐片直传（断点跳已传，每片独立签名+重试+重签）
    for (let batchStart = 1; batchStart <= totalChunks; batchStart += PRESIGN_BATCH) {
      if (abortController.aborted || signal?.aborted) throw new UploadAbortedError()

      const partNumbers = []
      for (let i = batchStart; i < batchStart + PRESIGN_BATCH && i <= totalChunks; i++) {
        if (!uploadedSet.has(i)) partNumbers.push(i)
      }
      if (!partNumbers.length) continue

      // 批内并发 3 片直传（每片自带重试/重签）
      const queue = [...partNumbers]
      const workers = Array.from({ length: 3 }, async () => {
        while (queue.length) {
          const partNumber = queue.shift()
          if (partNumber === undefined) break
          if (abortController.aborted || signal?.aborted) throw new UploadAbortedError()
          const start = (partNumber - 1) * S3_CHUNK_SIZE
          const blob = file.slice(start, Math.min(start + S3_CHUNK_SIZE, file.size))
          await putPartWithRetry(uploadId, partNumber, blob, signal)
          uploadedSet.add(partNumber)
          onProgress({
            phase: 'uploading',
            label: `上传中 ${uploadedSet.size}/${totalChunks}`,
            percent: Math.round((uploadedSet.size / totalChunks) * 95)
          })
        }
      })
      await Promise.all(workers)
    }

    // 4. 完成（服务端 ListParts 校验 + 组装 + ffprobe；带 quickHash 支持幂等）
    onProgress({ phase: 'completing', label: '合并校验中…', percent: 98 })
    const completeRes = await apiRequest(
      `/media/v2/complete-upload?uploadId=${uploadId}&quickHash=${quickHash}`,
      { method: 'POST', signal }
    )
    if (!completeRes.ok) throw new Error(await completeRes.text() || '合并失败')
    const completeData = await completeRes.json()

    onProgress({ phase: 'done', label: '上传完成', percent: 100 })
    return { mediaId: completeData.mediaId, instant: false }

  } catch (error) {
    // 主动取消：通知后端 abort（清掉 S3 multipart）
    if (error instanceof UploadAbortedError || error?.name === 'AbortError') {
      try {
        await apiRequest(`/media/v2/abort?uploadId=${uploadId}`, { method: 'POST' })
      } catch { /* abort 失败不影响 */ }
      throw new UploadAbortedError()
    }
    throw error
  }
}
