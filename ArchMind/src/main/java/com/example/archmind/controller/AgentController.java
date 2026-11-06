package com.example.archmind.controller;

import com.example.archmind.common.result.Result;
import com.example.archmind.dto.request.AgentAskRequest;
import com.example.archmind.dto.request.NavigateRequest;
import com.example.archmind.dto.request.ViewportReportRequest;
import com.example.archmind.dto.response.AgentResponse;
import com.example.archmind.dto.response.ConversationDTO;
import com.example.archmind.dto.response.ConversationHistoryDTO;
import com.example.archmind.dto.response.ConversationSummaryDTO;
import com.example.archmind.dto.response.ViewportAck;
import com.example.archmind.service.agent.AgentGatewayService;
import lombok.RequiredArgsConstructor;
import org.springframework.http.MediaType;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

import java.util.List;

/**
 * Agent 对话的**公网入口**（{@code /api/agent/*}），带 JWT。
 *
 * <p>它和 {@link InternalToolController} 是**相反方向**的两条边：</p>
 * <ul>
 *   <li>{@code InternalToolController}（{@code /internal/tools/*}）：Python → Java，取数据</li>
 *   <li>本控制器（{@code /api/agent/*}）：前端 → Java → Python，问问题</li>
 * </ul>
 *
 * <p>鉴权不用在这里写：{@code SecurityConfig} 里 {@code /api/**} 是
 * {@code authenticated()}，JWT 过滤器已经把关了。
 * <b>租户校验也不在这里</b>，在 {@code AgentGatewayService} —— 控制器只做转发。</p>
 */
@RestController
@RequestMapping("/api/agent")
@RequiredArgsConstructor
public class AgentController {

    private final AgentGatewayService agentGatewayService;

    /** 一轮问答。 */
    @PostMapping("/ask")
    public Result<AgentResponse> ask(@RequestBody AgentAskRequest req) {
        return Result.success(agentGatewayService.ask(req));
    }

    /**
     * 流式问答（SSE）。
     *
     * <p><b>全项目唯一不包 {@code Result} 的接口</b> —— 事件流本身就是响应体，
     * 再套一层 {@code {code,message,data}}，前端就没法逐帧解析了。</p>
     *
     * <p>控制器只做转发：租户校验、线程池、事件过滤都在网关里。</p>
     */
    @PostMapping(value = "/ask/stream", produces = MediaType.TEXT_EVENT_STREAM_VALUE)
    public SseEmitter askStream(@RequestBody AgentAskRequest req) {
        return agentGatewayService.askStream(req);
    }

    /**
     * 点按钮导航 —— <b>零 token</b>。
     *
     * <p>和 {@code /ask} 的区别是「谁判的意图」：{@code /ask} 靠 LLM 从自然语言里判出
     * 「这是导航」，这里前端**直接声明**，所以一次模型都不调。</p>
     */
    @PostMapping("/navigate")
    public Result<AgentResponse> navigate(@RequestBody NavigateRequest req) {
        return Result.success(agentGatewayService.navigate(req));
    }

    /** 前端上报屏幕状态。会话 id 走路径，转发时由网关注入进 body。 */
    @PostMapping("/conversations/{conversationId}/viewport")
    public Result<ViewportAck> viewport(@PathVariable String conversationId,
                                        @RequestBody ViewportReportRequest req) {
        return Result.success(agentGatewayService.reportViewport(conversationId, req));
    }

    /** 读回会话：刷新页面时恢复屏幕用。会话已过期时返回 {@code exists:false}。 */
    @GetMapping("/conversations/{conversationId}")
    public Result<ConversationDTO> getConversation(@PathVariable String conversationId,
                                                   @RequestParam Long projectId) {
        return Result.success(agentGatewayService.getConversation(conversationId, projectId));
    }

    /** 会话索引：一个项目下可以有多个会话，不传 projectId 时跨项目。 */
    @GetMapping("/conversations")
    public Result<List<ConversationSummaryDTO>> listConversations(
            @RequestParam(required = false) Long projectId) {
        return Result.success(agentGatewayService.listConversations(projectId));
    }

    /** 会话历史：打开一个已存在的会话时恢复消息列表。 */
    @GetMapping("/conversations/{conversationId}/messages")
    public Result<ConversationHistoryDTO> getConversationMessages(
            @PathVariable String conversationId,
            @RequestParam Long projectId) {
        return Result.success(
                agentGatewayService.getConversationMessages(conversationId, projectId));
    }
}
