package com.example.server.utils;

import com.dynatrace.hash4j.hashing.HashStream128;
import com.dynatrace.hash4j.hashing.HashValue128;
import com.dynatrace.hash4j.hashing.Hashing;

import java.io.BufferedInputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.io.InputStream;

/**
 * quickHash（XXH3-128 全文件指纹）统一计算入口。
 * 与前端 hash-wasm createXXHash128(0) 流式输出逐位一致（hex = high64 + low64 各 16 位小写），
 * 手动上传（前端算）与 URL 导入（服务端算）因此共用同一套判重 key。
 * MD5 已废弃（2026-09-01 起 content_hash 列停止写入，仅保留历史数据）。
 */
public final class QuickHashUtils {

    private static final int BUFFER_SIZE = 1 << 20; // 1MB

    private QuickHashUtils() {
    }

    public static String xxh3Hex(File file) throws IOException {
        try (InputStream in = new BufferedInputStream(new FileInputStream(file))) {
            return xxh3Hex(in);
        }
    }

    public static String xxh3Hex(InputStream in) throws IOException {
        HashStream128 stream = Hashing.xxh3_128().hashStream();
        byte[] buf = new byte[BUFFER_SIZE];
        int n;
        while ((n = in.read(buf)) > 0) {
            stream.putBytes(buf, 0, n);
        }
        HashValue128 value = stream.get();
        return String.format("%016x%016x", value.getMostSignificantBits(), value.getLeastSignificantBits());
    }
}
