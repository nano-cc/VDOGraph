package com.example.server.controller;

import com.example.server.common.Result;
import com.example.server.service.AuthService;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestAttribute;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.HashMap;
import java.util.Map;

/**
 * 模型供应商配置（管理员）
 * 配置存 Redis（config:models），Python AI 服务运行时读取（30s 内生效，无需重启）
 * 仅支持 OpenAI 兼容端点（base_url + api_key + model）
 */
@RestController
@RequestMapping("/admin/config")
public class ModelConfigController {

    private static final Logger log = LoggerFactory.getLogger(ModelConfigController.class);
    private static final String REDIS_KEY = "config:models";
    private static final String[] KINDS = {"llm", "embedding", "asr", "reranker"};

    private final StringRedisTemplate redisTemplate;
    private final AuthService authService;
    private final ObjectMapper objectMapper;

    public ModelConfigController(StringRedisTemplate redisTemplate,
                                 AuthService authService,
                                 ObjectMapper objectMapper) {
        this.redisTemplate = redisTemplate;
        this.authService = authService;
        this.objectMapper = objectMapper;
    }

    @GetMapping("/models")
    public Result<Map<String, Object>> getModels(
            @RequestAttribute(AuthService.REQUEST_USER_ID) Long userId) throws Exception {
        authService.requireAdmin(userId);
        Map<Object, Object> raw = redisTemplate.opsForHash().entries(REDIS_KEY);
        Map<String, Object> result = new HashMap<>();
        for (Map.Entry<Object, Object> entry : raw.entrySet()) {
            // 返回时脱敏 api_key
            Map<?, ?> cfg = objectMapper.readValue((String) entry.getValue(), Map.class);
            Map<String, Object> masked = new HashMap<>();
            for (Map.Entry<?, ?> cfgEntry : cfg.entrySet()) {
                masked.put(String.valueOf(cfgEntry.getKey()), cfgEntry.getValue());
            }
            Object key = cfg.get("api_key");
            if (key instanceof String k && k.length() > 8) {
                masked.put("api_key", k.substring(0, 4) + "****" + k.substring(k.length() - 4));
            }
            result.put((String) entry.getKey(), masked);
        }
        return Result.ok(result);
    }

    @PutMapping("/models")
    public Result<Void> saveModels(
            @RequestBody Map<String, Map<String, String>> configs,
            @RequestAttribute(AuthService.REQUEST_USER_ID) Long userId) throws Exception {
        authService.requireAdmin(userId);
        for (Map.Entry<String, Map<String, String>> entry : configs.entrySet()) {
            String kind = entry.getKey();
            if (!isValidKind(kind)) {
                continue;
            }
            Map<String, String> cfg = entry.getValue();
            if (cfg.get("model") == null || cfg.get("model").isBlank()) {
                continue;
            }
            // api_key 脱敏值不覆盖（前端回填的是脱敏后的）
            Object existing = redisTemplate.opsForHash().get(REDIS_KEY, kind);
            if (existing != null && cfg.get("api_key") != null && cfg.get("api_key").contains("****")) {
                Map<?, ?> old = objectMapper.readValue((String) existing, Map.class);
                cfg.put("api_key", (String) old.get("api_key"));
            }
            redisTemplate.opsForHash().put(REDIS_KEY, kind, objectMapper.writeValueAsString(cfg));
            log.info("Model config updated by user {}: {} -> {} ({})", userId, kind, cfg.get("model"), cfg.get("base_url"));
        }
        return Result.ok();
    }

    /**
     * 测试供应商连通性（OpenAI 兼容端点的 /models）
     */
    @PostMapping("/models/test")
    public Result<String> testConnection(
            @RequestBody Map<String, String> cfg,
            @RequestAttribute(AuthService.REQUEST_USER_ID) Long userId) {
        authService.requireAdmin(userId);
        String baseUrl = cfg.get("base_url");
        String apiKey = cfg.get("api_key");
        if (baseUrl == null || baseUrl.isBlank()) {
            return Result.error(400, "base_url 不能为空");
        }
        try {
            HttpClient client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(10)).build();
            HttpRequest request = HttpRequest.newBuilder()
                    .uri(URI.create(baseUrl.replaceAll("/$", "") + "/models"))
                    .header("Authorization", "Bearer " + (apiKey == null ? "" : apiKey))
                    .timeout(Duration.ofSeconds(15))
                    .GET()
                    .build();
            HttpResponse<String> response = client.send(request, HttpResponse.BodyHandlers.ofString());
            if (response.statusCode() == 200) {
                return Result.ok("连接成功");
            }
            return Result.error(response.statusCode(), "供应商返回 HTTP " + response.statusCode());
        } catch (Exception e) {
            log.warn("Model config test failed: {}", e.getMessage());
            return Result.error(500, "连接失败: " + e.getMessage());
        }
    }

    private static boolean isValidKind(String kind) {
        for (String k : KINDS) {
            if (k.equals(kind)) return true;
        }
        return false;
    }
}
