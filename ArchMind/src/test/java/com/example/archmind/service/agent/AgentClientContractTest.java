package com.example.archmind.service.agent;

import com.example.archmind.config.AgentProperties;
import com.example.archmind.dto.request.NavigateRequest;
import com.example.archmind.dto.request.ViewportReportRequest;
import com.example.archmind.dto.response.AgentResponse;
import com.example.archmind.dto.response.ConversationDTO;
import com.example.archmind.dto.response.ViewportAck;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.net.Socket;

import static org.junit.jupiter.api.Assertions.*;
import static org.junit.jupiter.api.Assumptions.assumeTrue;

/**
 * Java → Python 这条边的**契约测试**：打真的是 Python 服务，不是 mock。
 *
 * <p>这一层要验的是单测覆盖不到的东西 —— <b>路径对不对、内部 token 头有没有带上、
 * Java 的 DTO 能不能反序列化 Python 真的发出来的 JSON</b>。
 * 尤其是最后一条：Spring Boot 默认忽略未知字段，DTO 少写一个字段**不会报错、只会静默丢**，
 * 只有打真服务才能发现。</p>
 *
 * <p><b>agent-service 没起就跳过</b>（{@code assumeTrue}）—— 与 Python 侧
 * {@code conftest.py} 里「Java 不可达就 skip」是同一条铁律：这组测试的红
 * 只应该代表「两边对不上了」，不该因为本机没起 Python 就红。</p>
 *
 * <p>跑法：先起 agent-service（{@code ./.venv/Scripts/python.exe -m uvicorn app.main:app --port 8000}），
 * 再 {@code ./mvnw test -Dtest=AgentClientContractTest}。</p>
 */
class AgentClientContractTest {

    private static final String HOST = "localhost";
    private static final int PORT = 8000;
    /** 与 Python 侧 .env 的 INTERNAL_TOKEN、Java 侧 application.yml 的默认值一致 */
    private static final String TOKEN = "dev-internal-token-change-me";

    private AgentProperties props;
    private AgentClient client;

    @BeforeEach
    void setUp() {
        assumeTrue(agentAlive(), "agent-service 未启动（" + HOST + ":" + PORT + "），跳过契约测试");
        props = new AgentProperties();
        props.setBaseUrl("http://" + HOST + ":" + PORT);
        client = new AgentClient(props, TOKEN);
    }

    private static boolean agentAlive() {
        try (Socket s = new Socket()) {
            s.connect(new InetSocketAddress(HOST, PORT), 500);
            return true;
        } catch (IOException e) {
            return false;
        }
    }

    // ==================== 路径 + 头 + JSON 反序列化 ====================

    @Test
    void navigateReachesPythonAndDeserializesTheWholeResponse() {
        NavigateRequest req = new NavigateRequest();
        req.setProjectId(1001L);
        req.setConversationId("c_contract_probe");
        req.setOp("BACK");

        AgentResponse resp = client.navigate(req);

        // 会话不存在 → Python 说过期，不建空的
        assertTrue(resp.getSessionExpired(), "期望 sessionExpired=true（会话不存在）");
        assertEquals("c_contract_probe", resp.getConversationId());
        assertNotNull(resp.getMeta(), "meta 必须被反序列化出来");
        assertEquals("SESSION_GONE", resp.getMeta().getIntent());
        // 这一条同时证明了：路径对、X-Internal-Token 被接受、
        // 以及 sessionExpired/conversationId/meta.intent 这些字段在 Java DTO 里都存在
    }

    @Test
    void viewportReachesPythonWithConversationIdInBody() {
        // 对外是 POST /api/agent/conversations/{id}/viewport（id 在路径），
        // 对内是 POST /internal/agent/viewport（id 在 body）—— 这里验的就是这次翻译
        ViewportReportRequest req = new ViewportReportRequest();
        req.setProjectId(1001L);
        req.setConversationId("c_contract_probe");
        req.setRev(1);
        req.setVisibleNodes(java.util.List.of("com.x.A"));

        ViewportAck ack = client.reportViewport(req);

        // 会话不存在 → 拒绝，且**回带 rev=null**
        assertFalse(ack.getAccepted());
        assertEquals("SESSION_GONE", ack.getReason());
        assertNull(ack.getRev());
    }

    @Test
    void getConversationReachesPython() {
        ConversationDTO dto = client.getConversation("c_contract_probe", 1001L);
        assertFalse(dto.getExists());
        assertEquals("SESSION_GONE", dto.getReason());
    }

    @Test
    void evictReachesPython() {
        // 不存在的项目也应「成功」（删 0 个），证明短超时实例也能正常打通
        assertDoesNotThrow(() -> client.evictProject(999_999_999L));
    }

    // ==================== 错误分类 ====================

    @Test
    void wrongTokenIsReportedAsAuthErrorNotEmptyServiceDown() {
        AgentClient badToken = new AgentClient(props, "definitely-not-the-right-token");

        // ★ 关键是**异常类型**：401 必须被归成 AgentAuthException（配置错误，要响），
        //   不能被归成 AgentTransportException（那会让部署事故看起来像网络抖动）
        AgentAuthException e = assertThrows(AgentAuthException.class,
                () -> badToken.getConversation("c_contract_probe", 1001L));
        assertTrue(e.getMessage().contains("401"), "异常信息里应带上 401 便于排查: " + e.getMessage());
    }
}
