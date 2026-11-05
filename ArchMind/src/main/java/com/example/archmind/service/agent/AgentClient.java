package com.example.archmind.service.agent;

import com.example.archmind.config.AgentProperties;
import com.example.archmind.dto.request.AgentAskRequest;
import com.example.archmind.dto.request.NavigateRequest;
import com.example.archmind.dto.request.ViewportReportRequest;
import com.example.archmind.dto.response.AgentResponse;
import com.example.archmind.dto.response.ConversationDTO;
import com.example.archmind.dto.response.ViewportAck;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.client.JdkClientHttpRequestFactory;
import org.springframework.stereotype.Component;
import org.springframework.web.client.ResourceAccessException;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientResponseException;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.net.ProxySelector;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.Map;
import java.util.function.Supplier;

/**
 * Python agent-service 的客户端 —— <b>Java 侧唯一会讲 HTTP 的地方</b>。
 *
 * <p>边界只此一处：这之上全是 Java 对象，这之下是 Python 的 camelCase JSON。
 * 形状与 Python 侧 {@code app/tools/java_client.py} 对称（那边是 Python 唯一发 HTTP 的地方）。</p>
 *
 * <p>有两个不同超时的实例：<b>长</b>的给 {@code /ask} 这类会等 LLM 的接口，
 * <b>短</b>的给 evict 这类清理接口 —— 删项目不能被 Python 拖住。</p>
 */
@Component
@Slf4j
public class AgentClient {

    /** 与 Python 侧 {@code app/config.py} 的 {@code INTERNAL_TOKEN_HEADER} 是同一个头 */
    private static final String HEADER = "X-Internal-Token";

    private static final String PATH_ASK = "/internal/agent/ask";
    private static final String PATH_ASK_STREAM = "/internal/agent/ask/stream";
    private static final String PATH_NAVIGATE = "/internal/agent/navigate";
    private static final String PATH_VIEWPORT = "/internal/agent/viewport";
    private static final String PATH_CONVERSATION = "/internal/agent/conversations/{id}";
    private static final String PATH_EVICT = "/internal/agent/admin/evict";
    private static final String PATH_INDEX = "/internal/index";

    /** 建立 TCP 连接的上限。和读超时分开：连不上要快速失败，不能陪着等满 70s */
    private static final int CONNECT_TIMEOUT_MS = 5_000;

    private final RestClient longClient;
    private final RestClient shortClient;
    private final HttpClient streamClient;
    private final String baseUrl;
    private final String token;
    private final int streamTimeoutMs;

    public AgentClient(AgentProperties props, @Value("${internal.token}") String token) {
        this.baseUrl = props.getBaseUrl();
        this.token = token;
        this.streamTimeoutMs = props.getStreamTimeoutMs();
        // 流式必须用 JDK HttpClient 原生接口：RestClient 只能拿到「整个 body」，
        // 拿不到逐行读的能力，SSE 需要的是后者。
        this.streamClient = newHttpClient(CONNECT_TIMEOUT_MS);
        this.longClient = build(props, token, props.getTimeoutMs());
        this.shortClient = build(props, token, props.getEvictTimeoutMs());
        log.info("Agent 客户端就绪: baseUrl={} timeout={}ms streamTimeout={}ms evictTimeout={}ms",
                props.getBaseUrl(), props.getTimeoutMs(), props.getStreamTimeoutMs(),
                props.getEvictTimeoutMs());
    }

    private static RestClient build(AgentProperties props, String token, int timeoutMs) {
        HttpClient httpClient = newHttpClient(Math.min(CONNECT_TIMEOUT_MS, timeoutMs));
        JdkClientHttpRequestFactory factory = new JdkClientHttpRequestFactory(httpClient);
        factory.setReadTimeout(Duration.ofMillis(timeoutMs));

        return RestClient.builder()
                .baseUrl(props.getBaseUrl())
                .requestFactory(factory)
                .defaultHeader(HEADER, token)
                .build();
    }

    private static HttpClient newHttpClient(int connectTimeoutMs) {
        return HttpClient.newBuilder()
                // ★ 必须钉死 HTTP/1.1。JDK HttpClient 默认会尝试 **h2c 明文升级**
                //   （先发 Upgrade: h2c，再退回 HTTP/1.1）。uvicorn 用的 h11 **不认这个升级**，
                //   直接回 400「Invalid HTTP request received.」——
                //   表现为 Java 打不通报错却指向 Python，极难看出是协议协商的问题。
                //   （P4.3 的契约测试抓到的：AgentClientContractTest）
                .version(HttpClient.Version.HTTP_1_1)
                .connectTimeout(Duration.ofMillis(connectTimeoutMs))
                // ★ 显式关掉代理，直连。和 Python 侧 java_client.py 的 trust_env=False 是同一件事：
                //   Windows 上系统代理（本机是 Clash 挂在 127.0.0.1:7897）一旦被用上，
                //   连 http://localhost:8000 都会被丢给代理 —— 表现为超时，极难排查。
                //   服务间调用打的是固定地址，本来就不该走用户的桌面代理。
                .proxy(ProxySelector.of(null))
                .build();
    }

    // ==================== 五个调用 ====================

    /** 一轮问答。**会等 LLM**，走长超时。 */
    public AgentResponse ask(AgentAskRequest req) {
        return execute(() -> longClient.post().uri(PATH_ASK).body(req)
                .retrieve().body(AgentResponse.class), "ask");
    }

    /** 零 LLM 导航。走长超时（虽然它快，但没必要为它单开一个实例）。 */
    public AgentResponse navigate(NavigateRequest req) {
        return execute(() -> longClient.post().uri(PATH_NAVIGATE).body(req)
                .retrieve().body(AgentResponse.class), "navigate");
    }

    /** 前端上报屏幕状态。 */
    public ViewportAck reportViewport(ViewportReportRequest req) {
        return execute(() -> longClient.post().uri(PATH_VIEWPORT).body(req)
                .retrieve().body(ViewportAck.class), "viewport");
    }

    /** 读回会话（刷新页面恢复屏幕用）。projectId / userId 走 query 参数 —— Python 那边是这么收的。 */
    public ConversationDTO getConversation(String conversationId, Long projectId, Long userId) {
        return execute(() -> longClient.get()
                .uri(PATH_CONVERSATION + "?projectId={pid}&userId={uid}",
                        conversationId, projectId, userId)
                .retrieve().body(ConversationDTO.class), "conversations");
    }

    /**
     * 流式问答的接收端。网关实现它，把事件搬进 {@code SseEmitter}。
     *
     * <p>{@link #alive()} 让读取循环能在客户端断开后立刻停下 ——
     * 否则用户已经关掉面板，这边还在把整轮跑完、烧 token。</p>
     */
    public interface SseSink {
        boolean alive();

        void event(SseEvent event);

        void complete();

        void error(Throwable error);
    }

    /**
     * 流式问答。**阻塞到上游结束**，调用方必须放到独立线程执行。
     *
     * <p>请求体由网关序列化好传进来：这一层只管 HTTP，不认识 DTO。</p>
     */
    public void streamAsk(String body, SseSink sink) {
        HttpRequest request;
        try {
            request = HttpRequest.newBuilder(URI.create(baseUrl + PATH_ASK_STREAM))
                    .header(HEADER, token)
                    .header("Content-Type", "application/json")
                    .header("Accept", "text/event-stream")
                    .timeout(Duration.ofMillis(streamTimeoutMs))
                    .POST(HttpRequest.BodyPublishers.ofString(body, StandardCharsets.UTF_8))
                    .build();
        } catch (RuntimeException e) {
            sink.error(e);
            return;
        }

        try {
            HttpResponse<InputStream> response =
                    streamClient.send(request, HttpResponse.BodyHandlers.ofInputStream());
            try (InputStream in = response.body()) {
                if (response.statusCode() != 200) {
                    sink.error(new AgentTransportException(
                            "对话服务返回 HTTP " + response.statusCode()));
                    return;
                }
                SseBlockDecoder decoder = new SseBlockDecoder();
                BufferedReader reader = new BufferedReader(
                        new InputStreamReader(in, StandardCharsets.UTF_8));
                String line;
                while (sink.alive() && (line = reader.readLine()) != null) {
                    decoder.accept(line).ifPresent(sink::event);
                }
            }
            if (sink.alive()) {
                sink.complete();
            }
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            sink.error(e);
        } catch (IOException | RuntimeException e) {
            sink.error(e);
        }
    }

    /**
     * 项目删除时清掉它的全部会话 Context。**走短超时**。
     *
     * <p>响应（{@code {"ok":true,...}}）没有调用方需要的字段，直接丢弃 ——
     * 这是个「尽力而为」的清理，调用方只关心有没有炸。</p>
     */
    public void evictProject(Long projectId) {
        execute(() -> shortClient.post().uri(PATH_EVICT).body(Map.of("projectId", projectId))
                .retrieve().toBodilessEntity(), "evict");
    }

    /**
     * 通知 Agent 侧为该项目建/重建向量索引。**走短超时**：Python 收到后异步执行，
     * 这里只负责把请求送到；送不到也不影响补齐结果，索引侧可以重跑。
     */
    public void triggerIndex(Long projectId, long rev) {
        execute(() -> shortClient.post().uri(PATH_INDEX)
                .body(Map.of("projectId", projectId, "rev", rev))
                .retrieve().toBodilessEntity(), "index");
    }

    // ==================== 唯一的错误分类点 ====================

    /**
     * 发请求、收响应、把异常翻译成三种语义。
     *
     * <p><b>不能只看「有没有炸」</b>：401 是配置错（要响），4xx 是参数错（用户改一下），
     * 超时/5xx 是对面不行（等会儿再试）。三者对用户和对运维的含义完全不同。</p>
     *
     * <p><b>不重试</b>：{@code /ask} 会写 Redis 且烧 token，重试等于重复计费 + 状态漂移
     * （Python 那边每次 {@code save} 都会重设 TTL 并压一条 history）。</p>
     */
    private <T> T execute(Supplier<T> call, String what) {
        try {
            return call.get();
        } catch (RestClientResponseException e) {
            throw classify(e, what);
        } catch (ResourceAccessException e) {
            // 超时 / 连接被拒 / 连接重置 —— 都没拿到响应
            log.warn("Agent 不可达 [{}]: {}", what, e.getMessage());
            throw new AgentTransportException("连不上对话服务（" + what + "）", e);
        }
    }

    private AgentClientException classify(RestClientResponseException e, String what) {
        int status = e.getStatusCode().value();

        if (status == 401) {
            // 配置错误 —— 必须响。降级成「服务不可用」会让部署事故看起来像网络抖动。
            log.error("Agent 内部鉴权失败 [{}] —— 查两边 INTERNAL_TOKEN 是否一致: {}",
                    what, e.getResponseBodyAsString());
            throw new AgentAuthException(
                    "Agent 侧拒绝鉴权（401）：internal.token 与 Agent 服务的 INTERNAL_TOKEN 不一致");
        }

        String body = e.getResponseBodyAsString();
        if (e.getStatusCode().is4xxClientError()) {
            // 多半是用户传了非法参数（如 navigate 的 op 填了枚举外的值），
            // 也可能是 Java 的 DTO 与 Python 的模型漂移了。
            log.warn("Agent 拒绝请求 [{}] HTTP {}: {}", what, status, body);
            throw new AgentBadRequestException("对话请求被拒绝（HTTP " + status + "）");
        }

        log.warn("Agent 服务异常 [{}] HTTP {}: {}", what, status, body);
        throw new AgentTransportException("对话服务返回 HTTP " + status);
    }
}
