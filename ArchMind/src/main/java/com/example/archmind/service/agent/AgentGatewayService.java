package com.example.archmind.service.agent;

import com.example.archmind.common.exception.BusinessException;
import com.example.archmind.common.util.CheckProjectUtil;
import com.example.archmind.dto.AgentNodeRef;
import com.example.archmind.dto.request.AgentAskRequest;
import com.example.archmind.dto.request.NavigateRequest;
import com.example.archmind.dto.request.ViewportReportRequest;
import com.example.archmind.dto.response.AgentResponse;
import com.example.archmind.dto.response.ConversationDTO;
import com.example.archmind.dto.response.ViewportAck;
import com.example.archmind.service.ast.CodeGraphQueryService;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.stereotype.Service;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;
import tools.jackson.databind.ObjectMapper;

import java.io.IOException;
import java.util.List;
import java.util.Objects;
import java.util.Set;
import java.util.concurrent.Executor;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.stream.Collectors;

/**
 * 前端与 Python agent-service 之间的网关层。
 *
 * <p><b>它存在的理由不是「加个代理」，而是补上租户边界。</b>Python 拿到
 * {@code projectId} 时不做任何归属检查 —— 内部工具接口用的是
 * {@code checkProjectUtil.checkProjectExists}，只问「这个项目存不存在」。
 * 它敢这么写，前提是「projectId 只由 Java 注入」。P4.3 之前这个前提不成立：
 * 前端没有正规入口，只能直连 Python 并塞任意 projectId。</p>
 *
 * <p>所以这里每条路都先 {@link CheckProjectUtil#checkProject(Long)}
 * —— 那个是<b>带归属校验</b>的版本（校验不过抛
 * {@code BusinessException("无权操作该项目")}），校验通过才把 projectId 交给 Python。</p>
 */
@Service
@Slf4j
public class AgentGatewayService {

    private final AgentClient agentClient;
    private final CheckProjectUtil checkProjectUtil;
    private final CodeGraphQueryService codeGraphQueryService;
    private final ObjectMapper objectMapper;
    private final Executor streamExecutor;

    public AgentGatewayService(AgentClient agentClient,
                               CheckProjectUtil checkProjectUtil,
                               CodeGraphQueryService codeGraphQueryService,
                               ObjectMapper objectMapper,
                               @Qualifier("agentStreamExecutor") Executor streamExecutor) {
        this.agentClient = agentClient;
        this.checkProjectUtil = checkProjectUtil;
        this.codeGraphQueryService = codeGraphQueryService;
        this.objectMapper = objectMapper;
        this.streamExecutor = streamExecutor;
    }

    /** 一轮问答。 */
    public AgentResponse ask(AgentAskRequest req) {
        checkProjectUtil.checkProject(req.getProjectId());
        // ★ R5：身份显式注入。Python 直连 MySQL，没有 userId 就无法按用户隔离会话与记忆。
        req.setUserId(currentUserIdOrThrow());
        return filterUiCommands(agentClient.ask(req), req.getProjectId());
    }

    /**
     * 流式问答。**返回时请求线程立刻释放**，上游读取在独立线程池里跑。
     *
     * <p>租户校验必须发生在<b>任何上游调用之前</b>（和 {@link #ask} 同一条边界）。
     * 区别是：一旦开始推流，就没有 HTTP 状态码可以表达失败了 ——
     * 所以上游异常统一变成一帧 {@code error}。</p>
     */
    public SseEmitter askStream(AgentAskRequest req) {
        checkProjectUtil.checkProject(req.getProjectId());
        req.setUserId(currentUserIdOrThrow());

        Long projectId = req.getProjectId();
        SseEmitter emitter = new SseEmitter(0L);
        AtomicBoolean alive = new AtomicBoolean(true);
        emitter.onCompletion(() -> alive.set(false));
        emitter.onTimeout(() -> {
            alive.set(false);
            emitter.complete();
        });

        String body;
        try {
            body = objectMapper.writeValueAsString(req);
        } catch (RuntimeException e) {
            throw new AgentBadRequestException("对话请求序列化失败");
        }

        try {
            streamExecutor.execute(() -> agentClient.streamAsk(body, new AgentClient.SseSink() {
                @Override
                public boolean alive() {
                    return alive.get();
                }

                @Override
                public void event(SseEvent event) {
                    String data;
                    try {
                        data = prepareEvent(event, projectId);
                    } catch (RuntimeException e) {
                        log.warn("事件转发失败 [{}] projectId={}: {}",
                                event.name(), projectId, e.getMessage());
                        return;
                    }
                    try {
                        emitter.send(SseEmitter.event().name(event.name()).data(data));
                    } catch (IOException | IllegalStateException e) {
                        // 客户端走了：停掉读取循环，别再烧 token
                        alive.set(false);
                    }
                }

                @Override
                public void complete() {
                    emitter.complete();
                }

                @Override
                public void error(Throwable error) {
                    log.warn("流式对话中断 projectId={}: {}", projectId, error.getMessage());
                    try {
                        emitter.send(SseEmitter.event().name("error")
                                .data("{\"message\":\"对话服务暂时不可用，请稍后再试\"}"));
                    } catch (IOException | IllegalStateException ignored) {
                        // 已经断开，没有可做的了
                    }
                    emitter.complete();
                }
            }));
        } catch (RejectedExecutionException e) {
            log.warn("流式问答排队已满，拒绝 projectId={}", projectId);
            try {
                emitter.send(SseEmitter.event().name("error")
                        .data("{\"message\":\"当前对话较多，请稍后再试\"}"));
            } catch (IOException | IllegalStateException ignored) {
                // 已经断开
            }
            emitter.complete();
        }
        return emitter;
    }

    /**
     * 按事件类型决定发什么。
     *
     * <p>只有 {@code answer} 需要解析：它带着 uiCommands，必须再过一次弱校验。
     * 其余事件（start / step / error）原样转发 —— 解析再序列化只多一次出错机会。</p>
     */
    String prepareEvent(SseEvent event, Long projectId) {
        if (!"answer".equals(event.name())) {
            return event.data();
        }
        AgentResponse resp = objectMapper.readValue(event.data(), AgentResponse.class);
        resp = filterUiCommands(resp, projectId);
        return objectMapper.writeValueAsString(resp);
    }

    /** 零 LLM 导航（前端点按钮）。 */
    public AgentResponse navigate(NavigateRequest req) {
        checkProjectUtil.checkProject(req.getProjectId());
        req.setUserId(currentUserIdOrThrow());
        // 导航这条路 Python 侧**没有**过 filter_ui_commands（它的 target 来自 focus_stack
        // 而不是本轮工具结果），所以 Java 这层是它唯一的校验 —— 照样要过一遍。
        return filterUiCommands(agentClient.navigate(req), req.getProjectId());
    }

    /**
     * 上报屏幕状态。
     *
     * <p>{@code conversationId} 取自**路径**，在这里注入进请求体 —— 这就是
     * 「对外 RESTful、对内扁平」的那次翻译（Python 那边的 viewport 接口把 id 放在 body 里）。</p>
     */
    public ViewportAck reportViewport(String conversationId, ViewportReportRequest req) {
        checkProjectUtil.checkProject(req.getProjectId());
        req.setUserId(currentUserIdOrThrow());
        // 以路径为准，覆盖请求体里可能带的同名值 —— 免得前端用一个 id 走路径、用另一个进 body
        req.setConversationId(conversationId);
        return agentClient.reportViewport(req);
    }

    /** 读回会话（刷新页面恢复屏幕用）。 */
    public ConversationDTO getConversation(String conversationId, Long projectId) {
        checkProjectUtil.checkProject(projectId);
        return agentClient.getConversation(conversationId, projectId, currentUserIdOrThrow());
    }

    /**
     * 取当前登录用户 id。
     *
     * <p>{@code /api/**} 已经过 JWT 过滤器，理论上必有值；取不到说明鉴权链出了问题，
     * 这里必须炸出来，而不是把 null 传给 Python（那边 userId 是必填 integer）。</p>
     */
    private Long currentUserIdOrThrow() {
        Long userId = checkProjectUtil.getCurrentUserId();
        if (userId == null) {
            throw new BusinessException("未登录或无法解析用户身份");
        }
        return userId;
    }

    // ==================== uiCommands 弱校验（设计 §3.1 决策 A）====================

    /**
     * 剔除指向**不存在的类节点**的出图命令。
     *
     * <p><b>这是第二道防线，不是真相源。</b>真相源是「本轮查过什么」（evidence），
     * 那个信息只在 Python 手里 —— 它调 Java 工具时拿到的。Java 想复刻就得缓存每轮
     * 发出去的 evidence，那等于把「无状态网关」拆掉。所以这里只做**弱校验**：
     * uid 是不是这个项目的节点。且**这是终态，不是过渡**。</p>
     *
     * <p><b>图查不动时放行</b>（见 catch）—— 见那里的说明。</p>
     */
    private AgentResponse filterUiCommands(AgentResponse resp, Long projectId) {
        List<AgentResponse.UiCommand> commands = resp.getUiCommands();
        if (commands == null || commands.isEmpty()) {
            // 澄清轮 / 工具答不了的那些分支本来就没有命令，别白查一次 Neo4j
            return resp;
        }

        Set<String> uids = commands.stream()
                .map(AgentResponse.UiCommand::getTarget)
                .filter(Objects::nonNull)
                .map(AgentNodeRef::getUid)
                .filter(Objects::nonNull)
                .collect(Collectors.toSet());
        if (uids.isEmpty()) {
            // 全是 CLEAR_VIEW 这类没有 target 的命令 —— 它们不受约束（Python 侧同理）
            return resp;
        }

        Set<String> existing;
        try {
            existing = codeGraphQueryService.existingClassUids(projectId, uids);
        } catch (Exception e) {
            // ★ 决策：图不可用时**放行，不剔除**。
            // 这层按设计只是「防御 Python 有 bug 的第二道」，不该把正确结果也拦掉。
            // 而且 NAVIGATE 轮的 uiCommand 来自 focus_stack 而非图查询 ——
            // **Neo4j 挂着的时候照样会有命令**，此时全部剔除等于把正常功能一起关掉。
            log.warn("uiCommands 弱校验跳过（图不可用），放行 {} 条命令 projectId={}: {}",
                    commands.size(), projectId, e.getMessage());
            return resp;
        }

        List<AgentResponse.UiCommand> kept = commands.stream()
                .filter(c -> c.getTarget() == null || existing.contains(c.getTarget().getUid()))
                .toList();
        if (kept.size() < commands.size()) {
            log.warn("剔除 {} 条指向不存在节点的 UICommand projectId={}：{}",
                    commands.size() - kept.size(), projectId,
                    commands.stream().map(AgentResponse.UiCommand::getTarget)
                            .filter(Objects::nonNull)
                            .map(AgentNodeRef::getUid)
                            .filter(u -> !existing.contains(u))
                            .toList());
        }
        resp.setUiCommands(kept);
        return resp;
    }
}
