package com.example.archmind.service.task;

import com.example.archmind.common.constant.ProjectAnalysisStatus;
import com.example.archmind.common.constant.TaskStatus;
import com.example.archmind.common.constant.TaskType;
import com.example.archmind.dao.TaskMapper;
import com.example.archmind.dto.response.DescribeBatchResult;
import com.example.archmind.dto.response.DescriptionStatusResponse;
import com.example.archmind.entity.Task;
import com.example.archmind.service.ClassDescriptionService;
import com.example.archmind.service.agent.AgentClient;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.scheduling.annotation.Async;
import org.springframework.stereotype.Component;
import tools.jackson.databind.ObjectMapper;

import java.time.LocalDateTime;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 分析成功后的描述补齐驱动：循环调用单批接口，直到没有待描述的类。
 *
 * <p>分析里的 describeStage 只覆盖 Top20 核心类，这里补的是全量。
 * 状态源只有「描述是否为空」——已写过的类不会再被选中，所以进程崩了、
 * 任务重跑都不会重复计费；Redis 只负责单批互斥和失败冷却。</p>
 *
 * <p>终止条件：remaining 归零（成功）、连续两次零写入（熔断）、轮数用尽、墙钟超时。</p>
 */
@Component
@RequiredArgsConstructor
@Slf4j
public class DescriptionBackfillExecutor {

    private static final int BATCH_LIMIT = 25;
    /** 理论轮数之外的安全余量：模型漏答的类要在后续轮次重试 */
    private static final int EXTRA_ROUNDS = 5;
    private static final int ZERO_WRITE_STOP = 2;
    private static final int MAX_CONSECUTIVE_FAILURES = 5;
    private static final long WALL_CLOCK_MS = 15L * 60 * 1000;
    private static final long BUSY_SLEEP_MS = 1_000L;
    private static final int ERROR_MAX_LEN = 500;

    private final ClassDescriptionService classDescriptionService;
    private final TaskMapper taskMapper;
    private final ObjectMapper objectMapper;
    private final AgentClient agentClient;

    @Async("descriptionExecutor")
    public void run(Long projectId, Long userId) {
        Task task = createTask(projectId, userId);
        try {
            loop(projectId, task);
        } catch (Throwable t) {
            log.error("描述补齐任务失败 projectId={}", projectId, t);
            finish(task.getId(), TaskStatus.FAILED, 0, null, truncate(t.getMessage()));
        }
    }

    private void loop(Long projectId, Task task) {
        DescriptionStatusResponse status = classDescriptionService.descriptionStatus(projectId);
        if (!ProjectAnalysisStatus.COMPLETED.equals(status.getAnalysisStatus())) {
            finish(task.getId(), TaskStatus.PARTIAL, progressOf(status), detail(status, 0, "analysis_not_completed"), null);
            return;
        }
        int pendingAtStart = status.getPending();
        if (pendingAtStart == 0) {
            finish(task.getId(), TaskStatus.SUCCESS, 100, detail(status, 0, "completed"), null);
            notifyIndexer(projectId, status.getRev());
            return;
        }

        int maxRounds = (int) Math.ceil(pendingAtStart / (double) BATCH_LIMIT) + EXTRA_ROUNDS;
        int rounds = 0;
        int zeroStreak = 0;
        int consecutiveFailures = 0;
        long deadline = System.currentTimeMillis() + WALL_CLOCK_MS;
        String stopReason = null;
        DescriptionStatusResponse latest = status;

        while (System.currentTimeMillis() < deadline) {
            DescribeBatchResult result = classDescriptionService.describePending(projectId, BATCH_LIMIT);
            String s = result.getStatus();

            if (DescribeBatchResult.STATUS_ANALYSIS_NOT_COMPLETED.equals(s)) {
                stopReason = s;
                break;
            }
            if (DescribeBatchResult.STATUS_ANALYSIS_RUNNING.equals(s)) {
                // 重分析已开始：这条链路的选批依据已经失效，交给分析成功后的新任务
                stopReason = "analysis_restarted";
                break;
            }
            if (DescribeBatchResult.STATUS_BUSY.equals(s)) {
                // 画布按需正占着锁，不计轮次
                sleep(BUSY_SLEEP_MS);
                continue;
            }
            if (DescribeBatchResult.STATUS_STALE.equals(s)) {
                // 图被重建，重新取批次即可
                continue;
            }
            if (DescribeBatchResult.STATUS_FAILED.equals(s)) {
                consecutiveFailures++;
                if (consecutiveFailures >= MAX_CONSECUTIVE_FAILURES) {
                    stopReason = "llm_unavailable";
                    break;
                }
                long retry = result.getRetryAfterMs() == null ? 5_000L : result.getRetryAfterMs();
                sleep(Math.max(1_000L, retry));
                continue;
            }

            // 正常执行了一批
            consecutiveFailures = 0;
            rounds++;
            boolean wroteAnything = result.getItems() != null && !result.getItems().isEmpty();
            zeroStreak = wroteAnything ? 0 : zeroStreak + 1;

            latest = classDescriptionService.descriptionStatus(projectId);
            updateProgress(task.getId(), progressOf(latest));

            if (result.getRemaining() != null && result.getRemaining() == 0) {
                stopReason = "completed";
                break;
            }
            if (zeroStreak >= ZERO_WRITE_STOP) {
                stopReason = "zero_write";
                break;
            }
            if (rounds >= maxRounds) {
                stopReason = "max_rounds";
                break;
            }
        }
        if (stopReason == null) {
            stopReason = System.currentTimeMillis() >= deadline ? "deadline" : "max_rounds";
        }

        latest = classDescriptionService.descriptionStatus(projectId);
        boolean done = latest.getPending() == 0;
        finish(task.getId(),
                done ? TaskStatus.SUCCESS : TaskStatus.PARTIAL,
                progressOf(latest),
                detail(latest, rounds, stopReason),
                null);
        log.info("描述补齐结束 projectId={}, rounds={}, pending={}, stoppedBy={}",
                projectId, rounds, latest.getPending(), stopReason);
        if (done) {
            notifyIndexer(projectId, latest.getRev());
        }
    }

    /**
     * 补齐完成后通知 Python 建索引。**尽力而为**：失败只告警，
     * 索引侧可以手动或定时重跑，不影响描述结果。
     */
    private void notifyIndexer(Long projectId, long rev) {
        try {
            agentClient.triggerIndex(projectId, rev);
        } catch (Exception e) {
            log.warn("触发向量索引失败 projectId={}: {}", projectId, e.getMessage());
        }
    }

    private Task createTask(Long projectId, Long userId) {
        Task task = new Task();
        task.setProjectId(projectId);
        task.setUserId(userId);
        task.setType(TaskType.DESCRIBE);
        task.setStatus(TaskStatus.RUNNING);
        task.setProgress(0);
        task.setCreateTime(LocalDateTime.now());
        task.setUpdateTime(LocalDateTime.now());
        taskMapper.insert(task);
        return task;
    }

    private void updateProgress(Long taskId, int progress) {
        Task upd = new Task();
        upd.setId(taskId);
        upd.setProgress(progress);
        upd.setUpdateTime(LocalDateTime.now());
        taskMapper.updateById(upd);
    }

    private void finish(Long taskId, String status, int progress, String detail, String error) {
        Task upd = new Task();
        upd.setId(taskId);
        upd.setStatus(status);
        upd.setProgress(progress);
        upd.setDetail(detail);
        upd.setError(error);
        upd.setUpdateTime(LocalDateTime.now());
        taskMapper.updateById(upd);
    }

    private int progressOf(DescriptionStatusResponse status) {
        if (status.getDescribable() <= 0) {
            return 100;
        }
        return Math.min(100, status.getDescribed() * 100 / status.getDescribable());
    }

    private String detail(DescriptionStatusResponse status, int rounds, String stoppedBy) {
        Map<String, Object> detail = new LinkedHashMap<>();
        detail.put("total", status.getTotal());
        detail.put("describable", status.getDescribable());
        detail.put("described", status.getDescribed());
        detail.put("pending", status.getPending());
        detail.put("rounds", rounds);
        detail.put("stoppedBy", stoppedBy);
        try {
            return objectMapper.writeValueAsString(detail);
        } catch (Exception e) {
            log.warn("序列化描述补齐结果失败 projectId={}", status.getProjectId(), e);
            return null;
        }
    }

    private void sleep(long millis) {
        try {
            Thread.sleep(millis);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new IllegalStateException("描述补齐被中断", e);
        }
    }

    private String truncate(String error) {
        if (error == null) {
            return "未知错误";
        }
        return error.length() <= ERROR_MAX_LEN ? error : error.substring(0, ERROR_MAX_LEN);
    }
}
