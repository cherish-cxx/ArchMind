package com.example.archmind.service.task;

import com.example.archmind.common.constant.AnalysisStage;
import com.example.archmind.dto.response.ProjectOverviewResponse;
import com.example.archmind.service.AnalysisPipelineService;
import com.example.archmind.service.TaskService;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.scheduling.annotation.Async;
import org.springframework.stereotype.Component;

/**
 * 异步入口：只做三件事——切阶段、驱动 pipeline、处理成败与释放锁。
 * 真正的业务算法在 AnalysisPipelineService 里，这里不含任何解析/LLM 逻辑。
 */
@Component
@RequiredArgsConstructor
@Slf4j
public class AnalysisTaskExecutor {

    private final TaskService taskService;
    private final AnalysisPipelineService pipeline;
    private final DescriptionBackfillExecutor descriptionBackfillExecutor;

    @Async("analysisExecutor")
//    开启异步任务
    public void run(Long taskId, Long projectId, Long userId) {
        log.info("分析任务开始 taskId={}, projectId={}", taskId, projectId);
        taskService.markRunning(taskId);
        boolean success = false;
        try {
            // 阶段一：文件扫描并更新任务中的状态
            taskService.enterStage(taskId, projectId, AnalysisStage.SCAN);
            pipeline.scanStage(projectId,
                    pct -> taskService.reportProgress(taskId, projectId, AnalysisStage.SCAN, pct));

            // 阶段二：AST 提取
            taskService.enterStage(taskId, projectId, AnalysisStage.AST);
            pipeline.astStage(projectId,
                    pct -> taskService.reportProgress(taskId, projectId, AnalysisStage.AST, pct));

            // 阶段三：概况生成
            taskService.enterStage(taskId, projectId, AnalysisStage.OVERVIEW);
            ProjectOverviewResponse overview = pipeline.overviewStage(projectId,
                    pct -> taskService.reportProgress(taskId, projectId, AnalysisStage.OVERVIEW, pct));

            // 阶段四：核心类简要描述（挑核心类 → LLM 批量生成一句话简介 → 写回图节点）
            taskService.enterStage(taskId, projectId, AnalysisStage.DESCRIBE);
            pipeline.describeStage(projectId,
                    pct -> taskService.reportProgress(taskId, projectId, AnalysisStage.DESCRIBE, pct));

            taskService.markSuccess(taskId, overview);
            success = true;
            log.info("分析任务完成 taskId={}, projectId={}", taskId, projectId);
        } catch (Throwable t) {
            // 异步方法抛出的异常没人接，会把任务永远卡在 RUNNING —— 必须在这里吞掉并落 FAILED
            log.error("分析任务失败 taskId={}, projectId={}", taskId, projectId, t);
            taskService.markFailed(taskId, t.getMessage());
        } finally {
            taskService.releaseLock(projectId);
        }

        // 必须等分析锁释放之后再投递：补齐任务的第一件事就是检查这把锁
        if (success) {
            try {
                descriptionBackfillExecutor.run(projectId, userId);
            } catch (Exception e) {
                log.warn("投递描述补齐任务失败 projectId={}", projectId, e);
            }
        }
    }
}
