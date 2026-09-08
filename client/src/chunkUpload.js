/**
 * 上传相关纯工具函数（#53：老分片上传协议已下线，手动上传统一走 s3Upload.js 的 S3 直传）。
 * 仅保留 App.vue 仍在用的格式化/校验工具。
 */

const CHUNK_SIZE = 5 * 1024 * 1024
const MAX_TOTAL_CHUNKS = 410

export const MAX_UPLOAD_BYTES = MAX_TOTAL_CHUNKS * CHUNK_SIZE

export function formatBytes(bytes) {
  const value = Number(bytes) || 0
  if (value < 1024) return `${value} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let scaled = value / 1024
  let unitIndex = 0
  while (scaled >= 1024 && unitIndex < units.length - 1) {
    scaled /= 1024
    unitIndex += 1
  }
  return `${scaled >= 10 ? Math.round(scaled) : scaled.toFixed(1)} ${units[unitIndex]}`
}

export function formatDurationText(seconds) {
  if (!Number.isFinite(seconds) || seconds <= 0) return ''
  const total = Math.round(seconds)
  if (total < 60) return `${total} 秒`
  const minutes = Math.floor(total / 60)
  if (minutes < 60) return `${minutes} 分 ${String(total % 60).padStart(2, '0')} 秒`
  return `${Math.floor(minutes / 60)} 小时 ${String(minutes % 60).padStart(2, '0')} 分`
}

/** 选择文件时的前置校验，避免进入上传态之后才失败。 */
export function validateVideoFile(file) {
  if (!file) return '请先选择视频文件'
  if (!file.size) return '该文件大小为 0，可能已损坏或仍在同步，请重新选择'
  if (file.size > MAX_UPLOAD_BYTES) {
    return `文件 ${formatBytes(file.size)}，超过 ${formatBytes(MAX_UPLOAD_BYTES)} 上限，请先压缩或分段`
  }
  return ''
}
