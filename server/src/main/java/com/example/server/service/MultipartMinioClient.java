package com.example.server.service;

import io.minio.MinioAsyncClient;
import io.minio.messages.InitiateMultipartUploadResult;
import io.minio.messages.ListMultipartUploadsResult;
import io.minio.messages.ListPartsResult;
import io.minio.messages.Part;

/**
 * MinIO multipart 操作封装
 * （minio-java 8.5.7：multipart 方法是 S3Base 的 protected 成员，
 *   MinioAsyncClient extends S3Base，子类化暴露出来）
 */
public class MultipartMinioClient extends MinioAsyncClient {

    public MultipartMinioClient(MinioAsyncClient other) {
        super(other);
    }

    public String createUpload(String bucket, String object) throws Exception {
        InitiateMultipartUploadResult result = createMultipartUpload(bucket, null, object, null, null).result();
        return result.uploadId();
    }

    public ListPartsResult listParts(String bucket, String object, String uploadId) throws Exception {
        return listParts(bucket, null, object, 1000, 0, uploadId, null, null).result();
    }

    public void completeUpload(String bucket, String object, String uploadId, Part[] parts) throws Exception {
        completeMultipartUpload(bucket, null, object, uploadId, parts, null, null);
    }

    public void abortUpload(String bucket, String object, String uploadId) throws Exception {
        abortMultipartUpload(bucket, null, object, uploadId, null, null);
    }

    /** 列出所有未完成的 multipart 上传（G4 孤儿清扫用） */
    public ListMultipartUploadsResult listIncompleteUploads(String bucket, String prefix) throws Exception {
        return listMultipartUploads(bucket, null, null, null, null, 1000, prefix, null, null, null).result();
    }
}
