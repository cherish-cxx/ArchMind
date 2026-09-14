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

    @Async("analysisExecutor")
//    开启异步任务
    public void run(Long taskId, Long projectId, Long userId) {
        log.info("分析任务开始 taskId={}, projectId={}", taskId, projectId);
        taskService.markRunning(taskId);
        try {
            // 阶段一：文件扫描并更新任务中的状态
            taskService.enterStage(taskId, AnalysisStage.SCAN);
            pipeline.scanStage(projectId,
                    pct -> taskService.reportProgress(taskId, AnalysisStage.SCAN, pct));

            // 阶段二：AST 提取
            taskService.enterStage(taskId, AnalysisStage.AST);
            pipeline.astStage(projectId,
                    pct -> taskService.reportProgress(taskId, AnalysisStage.AST, pct));

            // 阶段三：概况生成
            taskService.enterStage(taskId, AnalysisStage.OVERVIEW);
            ProjectOverviewResponse overview = pipeline.overviewStage(projectId,
                    pct -> taskService.reportProgress(taskId, AnalysisStage.OVERVIEW, pct));

            // 阶段四：核心类简要描述（挑核心类 → LLM 批量生成一句话简介 → 写回图节点）
            taskService.enterStage(taskId, AnalysisStage.DESCRIBE);
            pipeline.describeStage(projectId,
                    pct -> taskService.reportProgress(taskId, AnalysisStage.DESCRIBE, pct));

            taskService.markSuccess(taskId, overview);
            log.info("分析任务完成 taskId={}, projectId={}", taskId, projectId);
        } catch (Throwable t) {
            // 异步方法抛出的异常没人接，会把任务永远卡在 RUNNING —— 必须在这里吞掉并落 FAILED
            log.error("分析任务失败 taskId={}, projectId={}", taskId, projectId, t);
            taskService.markFailed(taskId, t.getMessage());
        } finally {
            taskService.releaseLock(projectId);
        }
    }
}
