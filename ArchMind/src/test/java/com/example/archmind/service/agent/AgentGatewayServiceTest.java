package com.example.archmind.service.agent;

import com.example.archmind.common.exception.BusinessException;
import com.example.archmind.common.util.CheckProjectUtil;
import com.example.archmind.dto.AgentNodeRef;
import com.example.archmind.dto.request.AgentAskRequest;
import com.example.archmind.dto.response.AgentResponse;
import com.example.archmind.service.ast.CodeGraphQueryService;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.mockito.junit.jupiter.MockitoSettings;
import org.mockito.quality.Strictness;

import java.util.ArrayList;
import java.util.List;
import java.util.Set;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyCollection;
import static org.mockito.Mockito.*;

/**
 * 网关层单测：**不起 Spring、不发真 HTTP、不连 Neo4j**。
 *
 * <p>这一层要钉住的是两件事：<b>租户校验一定在转发之前</b>，
 * 以及 <b>uiCommands 弱校验的三种结局</b>（剔除 / 放行 / 不查）。</p>
 */
@ExtendWith(MockitoExtension.class)
@MockitoSettings(strictness = Strictness.LENIENT)
class AgentGatewayServiceTest {

    private static final Long PID = 1001L;

    @Mock
    private AgentClient agentClient;
    @Mock
    private CheckProjectUtil checkProjectUtil;
    @Mock
    private CodeGraphQueryService codeGraphQueryService;

    private AgentGatewayService service;

    @BeforeEach
    void setUp() {
        service = new AgentGatewayService(agentClient, checkProjectUtil, codeGraphQueryService);
    }

    // ==================== 假数据构造 ====================

    private static AgentResponse.UiCommand cmd(String op, String uid) {
        AgentResponse.UiCommand c = new AgentResponse.UiCommand();
        c.setOp(op);
        if (uid != null) {
            AgentNodeRef t = new AgentNodeRef();
            t.setType("CLASS");
            t.setUid(uid);
            c.setTarget(t);
        }
        return c;
    }

    private static AgentResponse responseWith(AgentResponse.UiCommand... cmds) {
        AgentResponse r = new AgentResponse();
        r.setUiCommands(new ArrayList<>(List.of(cmds)));
        return r;
    }

    private static AgentAskRequest askRequest() {
        AgentAskRequest req = new AgentAskRequest();
        req.setProjectId(PID);
        req.setMessage("它的调用链呢");
        return req;
    }

    // ==================== 租户边界（P4 存在的理由）====================

    @Test
    void askChecksOwnershipBeforeForwarding() {
        when(agentClient.ask(any())).thenReturn(responseWith());

        service.ask(askRequest());

        // 顺序很重要：必须**先**校验归属，校验过了才轮到转发
        var order = inOrder(checkProjectUtil, agentClient);
        order.verify(checkProjectUtil).checkProject(PID);
        order.verify(agentClient).ask(any());
    }

    @Test
    void tenantFailureBlocksForwarding() {
        doThrow(new BusinessException("无权操作该项目"))
                .when(checkProjectUtil).checkProject(PID);

        BusinessException e = assertThrows(BusinessException.class, () -> service.ask(askRequest()));

        assertEquals("无权操作该项目", e.getMessage());
        // ★ 校验没过就**一步都不许往下走** —— 这正是「拿别人的项目 id」被挡住的地方
        verify(agentClient, never()).ask(any());
    }

    // ==================== uiCommands 弱校验 ====================

    @Test
    void dropsCommandsPointingToMissingNodes() {
        when(agentClient.ask(any())).thenReturn(
                responseWith(cmd("FOCUS_NODE", "com.x.A"), cmd("SHOW_PATH", "com.x.GONE")));
        when(codeGraphQueryService.existingClassUids(eq(PID), anyCollection()))
                .thenReturn(Set.of("com.x.A"));

        AgentResponse resp = service.ask(askRequest());

        assertEquals(1, resp.getUiCommands().size());
        assertEquals("com.x.A", resp.getUiCommands().get(0).getTarget().getUid());
    }

    @Test
    void keepsCommandsWithNoTarget() {
        // CLEAR_VIEW 没有 target —— 不受校验约束（Python 侧 filter_ui_commands 同理）
        when(agentClient.ask(any())).thenReturn(
                responseWith(cmd("CLEAR_VIEW", null), cmd("FOCUS_NODE", "com.x.GONE")));
        when(codeGraphQueryService.existingClassUids(eq(PID), anyCollection()))
                .thenReturn(Set.of());

        AgentResponse resp = service.ask(askRequest());

        assertEquals(1, resp.getUiCommands().size());
        assertEquals("CLEAR_VIEW", resp.getUiCommands().get(0).getOp());
    }

    @Test
    void passesEverythingThroughWhenGraphUnavailable() {
        // ★ 决策：图查不动时**放行**，不剔除。
        // NAVIGATE 轮的 uiCommand 来自 focus_stack 而非图查询 —— Neo4j 挂着时照样有命令，
        // 此时全部剔除等于把正常功能一起关掉。
        when(agentClient.ask(any())).thenReturn(responseWith(cmd("FOCUS_NODE", "com.x.A")));
        when(codeGraphQueryService.existingClassUids(eq(PID), anyCollection()))
                .thenThrow(new RuntimeException("Neo4j 连不上"));

        AgentResponse resp = service.ask(askRequest());

        assertEquals(1, resp.getUiCommands().size());
        assertEquals("com.x.A", resp.getUiCommands().get(0).getTarget().getUid());
    }

    @Test
    void skipsQueryWhenThereAreNoCommands() {
        // 澄清轮 / 工具答不了 —— 本来就没命令，不该为它白跑一次 Neo4j
        when(agentClient.ask(any())).thenReturn(responseWith());

        service.ask(askRequest());

        verifyNoInteractions(codeGraphQueryService);
    }

    @Test
    void skipsQueryWhenAllCommandsHaveNoTarget() {
        when(agentClient.ask(any())).thenReturn(responseWith(cmd("CLEAR_VIEW", null)));

        service.ask(askRequest());

        // 没有任何 uid 要验，同样不该开查询
        verify(codeGraphQueryService, never()).existingClassUids(any(), any());
    }

    // ==================== 导航那条路也要过校验 ====================

    @Test
    void navigateAlsoValidatesAndChecksOwnership() {
        // 导航这条路 Python 侧没有过 filter_ui_commands（target 来自 focus_stack），
        // 所以 Java 这层是它**唯一**的校验 —— 不能因为是「导航」就跳过。
        var navReq = new com.example.archmind.dto.request.NavigateRequest();
        navReq.setProjectId(PID);
        navReq.setConversationId("c_1");
        navReq.setOp("BACK");
        when(agentClient.navigate(any())).thenReturn(responseWith(cmd("FOCUS_NODE", "com.x.GONE")));
        when(codeGraphQueryService.existingClassUids(eq(PID), anyCollection()))
                .thenReturn(Set.of());

        AgentResponse resp = service.navigate(navReq);

        verify(checkProjectUtil).checkProject(PID);
        assertTrue(resp.getUiCommands().isEmpty());
    }
}
